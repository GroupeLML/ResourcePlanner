from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
import json

from sqlalchemy import delete, select, true
from sqlalchemy.orm import Session

from ...application.command_ports import ApprovedDemandSyncPort
from ...application.errors import ApplicationConflictError
from ...application.operational_contacts import OperationalContactService
from ...domain.operational_contacts import (
    PROVENANCE_APPROVAL_CAPTURE,
    PROVENANCE_LEGACY_UNKNOWN,
    PROVENANCE_OPERATIONAL_CAPTURE,
    RESPONSIBILITY_CONTEXT_VERSION,
)
from ...domain.active_days import split_total_workforce_hours
from ...domain.confirmation import CONFIRMATION_CONFIRMED, normalize_confirmation
from ...domain.demand_periods import PERIOD_KIND_CUMULATIVE
from .approval_revision_models import (
    APPROVAL_REFERENCE_CAPTURED,
    RequestApprovalRevision,
)
from .approval_revision_repository import SqlRequestApprovalRevisionRepository
from .base import utc_now
from .demand_period_models import (
    WorkforceRequestPeriod,
    WorkforceRequestPeriodRequirement,
    WorkforceRequestPeriodSelection,
)
from .estimated_days_sync import SqlEstimatedDaysApprovedDemandSyncAdapter
from .models import (
    ORIGIN_REQUEST,
    Project,
    RequestLine,
    RequestLineCompetency,
    Resource,
    ResourceRequirement,
    ResourceRequirementCompetency,
    Shift,
    TaskCatalogEntry,
    WorkforceRequest,
    WorkforceRequestHistory,
    WorkPackage,
)
from .request_plan_preparation import (
    PreparedRequirementSpec,
    SqlRequestPlanPreparer,
)
from .planning_version import SqlPlanningMutationVersionRepository
from .operational_contact_repository import SqlOperationalContactRepository
from .segment_repository import SqlSegmentRepository
from .segment_asset_guard import segment_asset_dependencies


def _text(value: object) -> str:
    return str(value or "").strip()


@dataclass(frozen=True, slots=True)
class _LineRequirementSpec:
    key: tuple[str, ...]
    line: RequestLine
    period: WorkforceRequestPeriod | None
    start_date: object
    end_date: object
    planned_hours: Decimal
    desired_active_days: int | None
    confirmation: str
    proposed_resource_id: str | None
    description: str
    source_effort_id: str | None
    required_resource_class: str | None
    required_competency: str | None
    competency_ids: tuple[str, ...]


class SqlPeriodAwareApprovedDemandSyncAdapter(ApprovedDemandSyncPort):
    """Materialize approved demand envelopes, including RequestLine-owned needs."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._segments = SqlSegmentRepository(session)
        self._legacy = SqlEstimatedDaysApprovedDemandSyncAdapter(session)
        self._approval_revisions = SqlRequestApprovalRevisionRepository(session)
        self._plan_preparer = SqlRequestPlanPreparer(session)
        self._approved_request_version_override: int | None = None
        self._responsibility_revision_id: str | None = None
        self._restore_approved_responsibility_only = False

    def _request(self, number: str) -> WorkforceRequest:
        wanted = _text(number)
        request = self._session.scalar(
            select(WorkforceRequest).where(
                (WorkforceRequest.legacy_demand_number == wanted)
                | (WorkforceRequest.id == wanted)
            )
        )
        if request is None:
            raise KeyError(f"Demande {wanted} introuvable après approbation")
        return request

    def _guarded_materialization_request(self, number: str) -> WorkforceRequest:
        # ADR-006/009: serialize every path capable of (re)materializing the active
        # plan, then refresh the request under that guard. A stale ORM identity from
        # before a concurrent cancellation must never resurrect terminal output.
        SqlPlanningMutationVersionRepository(self._session).acquire()
        wanted = _text(number)
        request = self._session.scalar(
            select(WorkforceRequest)
            .where(
                (WorkforceRequest.legacy_demand_number == wanted)
                | (WorkforceRequest.id == wanted)
            )
            .execution_options(populate_existing=True)
        )
        if request is None:
            raise KeyError(f"Demande {wanted} introuvable après approbation")
        if request.status == "Annulée" or request.cancellation_state == "ACCEPTED":
            raise ApplicationConflictError(
                "Une demande annulée ne peut pas être rematérialisée.",
                code="cancelled_demand_materialization_forbidden",
                context={
                    "demand_number": _text(request.legacy_demand_number) or request.id,
                    "status": request.status,
                    "cancellation_state": request.cancellation_state,
                },
            )
        return request

    @staticmethod
    def _emergency_materialization(request: WorkforceRequest) -> bool:
        return bool(request.emergency_override_active) and request.status == "Soumise"

    def _approved_task_id(
        self,
        request: WorkforceRequest,
        line: RequestLine | None,
    ) -> str | None:
        work_package_id = (
            line.work_package_id
            if line is not None and line.work_package_id
            else request.work_package_id
        )
        if work_package_id:
            work_package = self._session.get(WorkPackage, work_package_id)
            if work_package is not None and work_package.task_catalog_item_id:
                if (
                    line is not None
                    and line.task_catalog_item_id
                    and line.task_catalog_item_id != work_package.task_catalog_item_id
                ):
                    raise ApplicationConflictError(
                        "La ligne approuvée contredit la tâche ERP classée du WorkPackage.",
                        code="request_line_work_package_task_conflict",
                        context={
                            "request_line_id": line.id,
                            "work_package_id": work_package.id,
                        },
                    )
                return work_package.task_catalog_item_id
        if line is not None and line.task_catalog_item_id:
            return line.task_catalog_item_id
        code = _text(line.erp_task_code if line is not None else request.erp_task_code)
        if not code:
            return None
        project = self._session.get(Project, request.project_id)
        if project is None:
            return None
        return self._session.scalar(
            select(TaskCatalogEntry.id).where(
                TaskCatalogEntry.project_number == project.number,
                TaskCatalogEntry.task_code == code,
            )
        )

    def _approved_request_version(self, request: WorkforceRequest) -> int:
        return max(
            int(
                request.aggregate_version
                if self._approved_request_version_override is None
                else self._approved_request_version_override
            ),
            1,
        )

    def _revision_responsibility_context(
        self,
        line_id: str,
    ) -> tuple[RequestApprovalRevision, dict[str, object], str, int] | None:
        if not self._responsibility_revision_id:
            return None
        revision = self._session.get(
            RequestApprovalRevision,
            self._responsibility_revision_id,
        )
        if revision is None:
            raise ValueError("La révision approuvée de responsabilité est introuvable.")
        payload = json.loads(revision.payload_text)
        container = payload.get("operational_responsibility_context")
        if not isinstance(container, dict):
            return None
        entries = container.get("entries")
        if not isinstance(entries, dict):
            return None

        matches: list[dict[str, object]] = []
        for identity, context in entries.items():
            if not isinstance(context, dict):
                continue
            try:
                parts = json.loads(str(identity))
            except (TypeError, ValueError):
                continue
            if (
                isinstance(parts, list)
                and len(parts) == 3
                and _text(parts[1]) == line_id
            ):
                matches.append(context)
        if not matches:
            return None
        first = matches[0]
        if any(context != first for context in matches[1:]):
            raise ValueError(
                "Les alternatives approuvées d'une même ligne portent des "
                "contextes de responsabilité incohérents."
            )
        provenance = _text(container.get("provenance")) or PROVENANCE_APPROVAL_CAPTURE
        version = int(
            container.get("mechanism_version")
            or RESPONSIBILITY_CONTEXT_VERSION
        )
        return revision, first, provenance, version

    @staticmethod
    def _store_responsibility_snapshot(
        requirement: ResourceRequirement,
        *,
        contact_id: str | None,
        source_type: str | None,
        source_entity_id: str | None,
        status: str | None,
        diagnostics: tuple[str, ...],
        provenance: str,
        version: int,
    ) -> None:
        requirement.captured_operational_responsible_contact_id = contact_id
        requirement.captured_operational_responsible_source_type = source_type
        requirement.captured_operational_responsible_source_entity_id = source_entity_id
        requirement.captured_operational_responsible_status = status
        requirement.captured_operational_responsible_diagnostics = json.dumps(
            list(diagnostics),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        requirement.operational_responsibility_context_provenance = provenance
        requirement.operational_responsibility_context_version = version

    def _capture_current_responsibility(
        self,
        request: WorkforceRequest,
        requirement: ResourceRequirement,
        line: RequestLine | None,
        *,
        provenance: str,
    ) -> None:
        service = OperationalContactService(
            SqlOperationalContactRepository(self._session)
        )
        resolution = (
            service.resolve_request_line(line.id)
            if line is not None
            else service.resolve_workforce_request(request.id)
        ).operational_responsible
        self._store_responsibility_snapshot(
            requirement,
            contact_id=resolution.contact_id,
            source_type=resolution.source_type,
            source_entity_id=resolution.source_entity_id,
            status=resolution.status,
            diagnostics=resolution.diagnostics,
            provenance=provenance,
            version=RESPONSIBILITY_CONTEXT_VERSION,
        )

    def _capture_approved_contact_context(
        self,
        request: WorkforceRequest,
        requirement: ResourceRequirement,
        line: RequestLine | None,
    ) -> None:
        line_id = line.id if line is not None else request.id
        captured = self._revision_responsibility_context(line_id)
        if captured is not None:
            revision, context, provenance, version = captured
            requirement.approved_task_catalog_item_id = _text(
                context.get("approved_task_catalog_item_id")
            ) or None
            requirement.approved_operational_responsible_override_contact_id = _text(
                context.get("approved_request_override_contact_id")
            ) or None
            requirement.approved_request_version = revision.request_version
            requirement.approved_contact_context_status = "CAPTURED"
            diagnostics = tuple(
                str(item)
                for item in (context.get("diagnostics") or [])
                if str(item)
            )
            self._store_responsibility_snapshot(
                requirement,
                contact_id=_text(context.get("contact_id")) or None,
                source_type=_text(context.get("source_type")) or None,
                source_entity_id=_text(context.get("source_entity_id")) or None,
                status=_text(context.get("status")) or None,
                diagnostics=diagnostics,
                provenance=provenance,
                version=version,
            )
            return

        if self._restore_approved_responsibility_only:
            if (
                requirement.operational_responsibility_context_provenance
                != PROVENANCE_LEGACY_UNKNOWN
                or requirement.operational_responsibility_context_version is not None
            ):
                return
            requirement.approved_request_version = self._approved_request_version(request)
            requirement.approved_contact_context_status = "LEGACY_UNKNOWN"
            return

        requirement.approved_task_catalog_item_id = self._approved_task_id(
            request,
            line,
        )
        requirement.approved_operational_responsible_override_contact_id = (
            request.operational_responsible_override_contact_id
        )
        requirement.approved_request_version = self._approved_request_version(request)
        requirement.approved_contact_context_status = "CAPTURED"
        self._capture_current_responsibility(
            request,
            requirement,
            line,
            provenance=(
                PROVENANCE_OPERATIONAL_CAPTURE
                if self._emergency_materialization(request)
                else PROVENANCE_APPROVAL_CAPTURE
            ),
        )

    def _approved_context_details(
        self,
        request: WorkforceRequest,
        requirements: list[ResourceRequirement],
    ) -> str:
        payload = {
            "approved_request_version": self._approved_request_version(request),
            "context_status": "CAPTURED",
            "requirements": [
                {
                    "requirement_id": row.id,
                    "source_request_line_id": row.source_request_line_id,
                    "approved_task_catalog_item_id": row.approved_task_catalog_item_id,
                    "approved_operational_responsible_override_contact_id": (
                        row.approved_operational_responsible_override_contact_id
                    ),
                    "captured_operational_responsible_contact_id": (
                        row.captured_operational_responsible_contact_id
                    ),
                    "captured_operational_responsible_source_type": (
                        row.captured_operational_responsible_source_type
                    ),
                    "captured_operational_responsible_source_entity_id": (
                        row.captured_operational_responsible_source_entity_id
                    ),
                    "captured_operational_responsible_status": (
                        row.captured_operational_responsible_status
                    ),
                    "operational_responsibility_context_provenance": (
                        row.operational_responsibility_context_provenance
                    ),
                    "operational_responsibility_context_version": (
                        row.operational_responsibility_context_version
                    ),
                }
                for row in requirements
            ],
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def _active_periods(self, request_id: str) -> list[WorkforceRequestPeriod]:
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

    def _selected_period_ids(self, request_id: str) -> dict[str, str]:
        rows = self._session.scalars(
            select(WorkforceRequestPeriodSelection).where(
                WorkforceRequestPeriodSelection.workforce_request_id == request_id
            )
        ).all()
        return {row.alternative_group: row.period_id for row in rows}

    def _selected_period_ids_by_line(
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

    def _active_requirements(self, request_id: str) -> list[ResourceRequirement]:
        return list(
            self._session.scalars(
                select(ResourceRequirement)
                .where(
                    ResourceRequirement.workforce_request_id == request_id,
                    ResourceRequirement.status != "Annulé",
                )
                .order_by(ResourceRequirement.created_at, ResourceRequirement.id)
            ).all()
        )

    def _requirement_links(self, requirement_ids: set[str]) -> dict[str, str]:
        if not requirement_ids:
            return {}
        rows = self._session.scalars(
            select(WorkforceRequestPeriodRequirement).where(
                WorkforceRequestPeriodRequirement.resource_requirement_id.in_(requirement_ids)
            )
        ).all()
        return {row.resource_requirement_id: row.period_id for row in rows}

    def _create_requirement(
        self,
        *,
        request: WorkforceRequest,
        project: Project,
        period: WorkforceRequestPeriod,
        proposed: Resource | None,
        planned_hours: float,
    ) -> ResourceRequirement:
        identifier = self._segments.create(
            {
                "NoDemande": _text(request.legacy_demand_number) or request.id,
                "NumeroProjet": project.number,
                "Technicien": proposed.name if proposed is not None else None,
                "DateDebut": period.start_date,
                "DateFin": period.end_date,
                "HeuresPrevues": planned_hours,
                "JoursActifsCibles": period.desired_active_days,
                "Statut": "Planifié" if proposed is not None else "À assigner",
                "Description": (
                    period.note
                    or request.erp_task_label
                    or request.description
                    or "Période approuvée"
                ),
                "CompetenceRequise": request.required_competencies,
                "TypePlanification": "Flexible",
                "Priorite": request.priority or "Normale",
                "HorsHoraireAutorise": False,
                "OrigineSegment": ORIGIN_REQUEST,
                "Confirmation": normalize_confirmation(
                    period.confirmation,
                    default=CONFIRMATION_CONFIRMED,
                ),
                "ConfirmationOverride": False,
            }
        )
        requirement = self._session.scalar(
            select(ResourceRequirement).where(
                ResourceRequirement.legacy_segment_id == identifier
            )
        )
        if requirement is None:
            raise RuntimeError("Le besoin créé pour la période approuvée est introuvable.")
        self._session.add(
            WorkforceRequestPeriodRequirement(
                resource_requirement_id=requirement.id,
                period_id=period.id,
            )
        )
        self._session.flush()
        return requirement

    def _sync_legacy_periods(
        self,
        request: WorkforceRequest,
        periods: list[WorkforceRequestPeriod],
    ) -> None:
        project = self._session.get(Project, request.project_id)
        if project is None:
            raise KeyError(f"Projet {request.project_id} introuvable")

        selected = self._selected_period_ids(request.id)
        effective = [
            period
            for period in periods
            if period.kind == PERIOD_KIND_CUMULATIVE
            or (
                period.alternative_group is not None
                and selected.get(period.alternative_group) == period.id
            )
        ]
        effective_ids = {period.id for period in effective}

        current = self._active_requirements(request.id)
        links = self._requirement_links({row.id for row in current})
        by_period: dict[str, list[ResourceRequirement]] = defaultdict(list)
        for requirement in current:
            period_id = links.get(requirement.id)
            if period_id not in effective_ids:
                requirement.status = "Annulé"
                continue
            by_period[period_id].append(requirement)

        for period in effective:
            desired = max(int(period.resource_count or 1), 1)
            split_hours = split_total_workforce_hours(period.hours, desired)
            rows = by_period.get(period.id, [])
            proposed = (
                self._session.get(Resource, period.proposed_resource_id)
                if period.proposed_resource_id
                else None
            )
            if period.proposed_resource_id and proposed is None:
                raise KeyError(
                    f"Ressource proposée introuvable pour la période {period.period_key}."
                )

            while len(rows) < desired:
                index = len(rows)
                rows.append(
                    self._create_requirement(
                        request=request,
                        project=project,
                        period=period,
                        proposed=proposed if index == 0 else None,
                        planned_hours=split_hours[index],
                    )
                )

            if len(rows) > desired:
                ranked = sorted(
                    rows,
                    key=lambda row: (
                        0 if row.assigned_resource_id else 1,
                        row.created_at,
                        row.id,
                    ),
                )
                keep_ids = {row.id for row in ranked[:desired]}
                for requirement in rows:
                    if requirement.id not in keep_ids:
                        requirement.status = "Annulé"
                rows = [row for row in ranked if row.id in keep_ids]

            rows = sorted(
                rows,
                key=lambda row: (
                    0 if row.assigned_resource_id else 1,
                    row.created_at,
                    row.id,
                ),
            )
            inherited_confirmation = normalize_confirmation(
                period.confirmation,
                default=CONFIRMATION_CONFIRMED,
            )
            for index, requirement in enumerate(rows):
                if index == 0 and proposed is not None and not requirement.assigned_resource_id:
                    requirement.assigned_resource_id = proposed.id
                if requirement.assigned_resource_id:
                    requirement.status = "Planifié"
                elif requirement.status not in {"Terminé", "Annulé"}:
                    requirement.status = "À assigner"

                requirement.project_id = project.id
                requirement.start_date = period.start_date
                requirement.end_date = period.end_date
                requirement.planned_hours = Decimal(str(split_hours[index])).quantize(Decimal("0.01"))
                requirement.desired_active_days = period.desired_active_days
                requirement.description = (
                    period.note
                    or request.erp_task_label
                    or request.description
                    or ""
                )
                requirement.required_competency = request.required_competencies
                requirement.priority = request.priority or "Normale"
                requirement.origin = ORIGIN_REQUEST
                legacy_line = self._session.get(RequestLine, request.id)
                self._capture_approved_contact_context(
                    request,
                    requirement,
                    legacy_line,
                )
                if not requirement.confirmation_overridden:
                    requirement.confirmation = inherited_confirmation

        groups = sorted(
            {
                _text(period.alternative_group)
                for period in periods
                if period.alternative_group
            }
        )
        unresolved = [group for group in groups if group not in selected]
        emergency = self._emergency_materialization(request)
        self._session.add(
            WorkforceRequestHistory(
                workforce_request_id=request.id,
                action=(
                    "Synchronisation périodes urgente"
                    if emergency
                    else "Synchronisation périodes"
                ),
                status=request.status,
                comment=(
                    f"{len(effective)} période(s) effective(s) matérialisée(s) par dérogation urgente; "
                    f"{len(unresolved)} groupe(s) alternatif(s) non résolu(s)."
                    if emergency
                    else (
                        f"{len(effective)} période(s) effective(s), "
                        f"{len(unresolved)} groupe(s) alternatif(s) non résolu(s)."
                    )
                ),
                actor_name=(
                    request.emergency_override_by_name
                    if emergency
                    else request.approved_by_name
                ),
                details=self._approved_context_details(
                    request,
                    self._active_requirements(request.id),
                ),
                occurred_at=utc_now(),
            )
        )
        self._session.flush()

    def _active_lines(self, request_id: str) -> list[RequestLine]:
        return list(
            self._session.scalars(
                select(RequestLine)
                .where(
                    RequestLine.workforce_request_id == request_id,
                    RequestLine.active == true(),
                )
                .order_by(RequestLine.position, RequestLine.id)
            ).all()
        )

    def _line_competencies(
        self,
        line_ids: tuple[str, ...],
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
            line_id: tuple(dict.fromkeys(values))
            for line_id, values in grouped.items()
        }

    def _line_work_package_refs(
        self,
        lines: list[RequestLine],
    ) -> dict[str, str | None]:
        ids = {
            line.work_package_id
            for line in lines
            if line.work_package_id
        }
        packages = (
            self._session.scalars(select(WorkPackage).where(WorkPackage.id.in_(ids))).all()
            if ids
            else []
        )
        refs = {
            package.id: _text(package.legacy_effort_id) or package.id
            for package in packages
        }
        return {
            line.id: refs.get(line.work_package_id) if line.work_package_id else None
            for line in lines
        }

    def _line_specs(
        self,
        request: WorkforceRequest,
        lines: list[RequestLine],
        periods: list[WorkforceRequestPeriod],
    ) -> tuple[list[_LineRequirementSpec], int]:
        periods_by_line: dict[str, list[WorkforceRequestPeriod]] = defaultdict(list)
        for period in periods:
            if period.request_line_id:
                periods_by_line[period.request_line_id].append(period)

        selected = self._selected_period_ids_by_line(request.id)
        competency_ids = self._line_competencies(tuple(line.id for line in lines))
        work_package_refs = self._line_work_package_refs(lines)
        specs: list[_LineRequirementSpec] = []
        unresolved = 0

        for line in lines:
            if _text(line.kind) == "ASSET":
                continue
            if _text(line.kind) != "WORKFORCE":
                raise ValueError(
                    f"La ligne {line.id} de type {line.kind} ne peut pas être matérialisée."
                )
            line_periods = periods_by_line.get(line.id, [])
            required_text = _text(line.required_competencies_snapshot) or None
            required_resource_class = _text(line.required_resource_class) or None
            if line_periods:
                groups = {
                    _text(period.alternative_group)
                    for period in line_periods
                    if period.alternative_group
                }
                unresolved += sum(
                    1
                    for group in groups
                    if (line.id, group) not in selected
                )
                effective = [
                    period
                    for period in line_periods
                    if period.kind == PERIOD_KIND_CUMULATIVE
                    or (
                        period.alternative_group is not None
                        and selected.get((line.id, period.alternative_group)) == period.id
                    )
                ]
                for period in effective:
                    proposed_resource_id = (
                        period.proposed_resource_id or line.proposed_resource_id
                    )
                    specs.append(
                        _LineRequirementSpec(
                            key=("PERIOD", line.id, period.period_key),
                            line=line,
                            period=period,
                            start_date=period.start_date,
                            end_date=period.end_date,
                            planned_hours=Decimal(str(period.hours)).quantize(Decimal("0.01")),
                            desired_active_days=period.desired_active_days,
                            confirmation=normalize_confirmation(
                                period.confirmation,
                                default=CONFIRMATION_CONFIRMED,
                            ),
                            proposed_resource_id=proposed_resource_id,
                            description=(
                                _text(period.note)
                                or _text(line.description)
                                or _text(line.erp_task_label)
                                or _text(request.description)
                                or "Période approuvée"
                            ),
                            source_effort_id=work_package_refs.get(line.id),
                            required_resource_class=required_resource_class,
                            required_competency=required_text,
                            competency_ids=competency_ids.get(line.id, ()),
                        )
                    )
                continue

            if line.desired_start is None:
                raise ValueError(
                    f"La date de début de la ligne {line.id} est requise pour la matérialisation."
                )
            if line.estimated_hours is None or line.estimated_hours <= 0:
                raise ValueError(
                    f"Les heures de la ligne {line.id} sont requises pour la matérialisation."
                )
            specs.append(
                _LineRequirementSpec(
                    key=("LINE", line.id),
                    line=line,
                    period=None,
                    start_date=line.desired_start,
                    end_date=line.desired_end or line.desired_start,
                    planned_hours=Decimal(str(line.estimated_hours)).quantize(Decimal("0.01")),
                    desired_active_days=(
                        int(line.desired_active_days)
                        if line.desired_active_days is not None
                        else None
                    ),
                    confirmation=normalize_confirmation(
                        line.confirmation,
                        default=CONFIRMATION_CONFIRMED,
                    ),
                    proposed_resource_id=line.proposed_resource_id,
                    description=(
                        _text(line.description)
                        or _text(line.erp_task_label)
                        or _text(request.description)
                        or "Besoin approuvé"
                    ),
                    source_effort_id=work_package_refs.get(line.id),
                    required_resource_class=required_resource_class,
                    required_competency=required_text,
                    competency_ids=competency_ids.get(line.id, ()),
                )
            )

        keys = [spec.key for spec in specs]
        if len(keys) != len(set(keys)):
            raise ValueError(
                "Deux besoins effectifs d'une même demande partagent la même identité de ligne/période."
            )
        return specs, unresolved

    def _current_requirement_keys(
        self,
        current: list[ResourceRequirement],
    ) -> dict[tuple[str, ...], list[ResourceRequirement]]:
        requirement_ids = {row.id for row in current}
        links = self._requirement_links(requirement_ids)
        period_ids = set(links.values())
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
        grouped: dict[tuple[str, ...], list[ResourceRequirement]] = defaultdict(list)
        for requirement in current:
            line_id = _text(requirement.source_request_line_id)
            if not line_id:
                grouped[("ORPHAN", requirement.id)].append(requirement)
                continue
            period = period_by_id.get(links.get(requirement.id, ""))
            if period is not None:
                grouped[("PERIOD", line_id, period.period_key)].append(requirement)
            else:
                grouped[("LINE", line_id)].append(requirement)
        for rows in grouped.values():
            rows.sort(
                key=lambda row: (
                    0 if row.assigned_resource_id else 1,
                    row.created_at,
                    row.id,
                )
            )
        return grouped

    def _locked_shifts(
        self,
        requirement_ids: set[str],
    ) -> dict[str, list[Shift]]:
        if not requirement_ids:
            return {}
        rows = self._session.scalars(
            select(Shift)
            .where(
                Shift.resource_requirement_id.in_(requirement_ids),
                Shift.locked == true(),
            )
            .order_by(Shift.work_date, Shift.id)
        ).all()
        grouped: dict[str, list[Shift]] = defaultdict(list)
        for shift in rows:
            grouped[shift.resource_requirement_id].append(shift)
        return grouped

    @staticmethod
    def _validate_locked_requirement(
        requirement: ResourceRequirement,
        spec: _LineRequirementSpec | None,
        locked: list[Shift],
    ) -> None:
        if not locked:
            return
        if spec is None:
            raise ValueError(
                "La réapprobation supprimerait un besoin contenant des quarts verrouillés. "
                "Libère ou déplace ces quarts manuels avant d'approuver."
            )
        for shift in locked:
            if shift.work_date < spec.start_date or shift.work_date > spec.end_date:
                raise ValueError(
                    "La nouvelle fenêtre d'une ligne exclut un quart verrouillé existant. "
                    "Libère ou déplace ce quart avant d'approuver."
                )
        locked_hours = sum((shift.hours for shift in locked), Decimal("0"))
        if locked_hours > spec.planned_hours + Decimal("0.001"):
            raise ValueError(
                "Les heures verrouillées dépassent les heures prévues par la nouvelle ligne. "
                "Réduis ou libère les quarts manuels avant d'approuver."
            )

    def _replace_requirement_competencies(
        self,
        requirement: ResourceRequirement,
        competency_ids: tuple[str, ...],
    ) -> None:
        self._session.execute(
            delete(ResourceRequirementCompetency).where(
                ResourceRequirementCompetency.resource_requirement_id == requirement.id
            )
        )
        for competency_id in competency_ids:
            self._session.add(
                ResourceRequirementCompetency(
                    resource_requirement_id=requirement.id,
                    competency_id=competency_id,
                )
            )

    def _update_period_link(
        self,
        requirement: ResourceRequirement,
        period: WorkforceRequestPeriod | None,
    ) -> None:
        link = self._session.get(WorkforceRequestPeriodRequirement, requirement.id)
        if period is None:
            if link is not None:
                self._session.delete(link)
            return
        if link is None:
            self._session.add(
                WorkforceRequestPeriodRequirement(
                    resource_requirement_id=requirement.id,
                    period_id=period.id,
                )
            )
        else:
            link.period_id = period.id

    def _apply_line_spec(
        self,
        request: WorkforceRequest,
        project: Project,
        requirement: ResourceRequirement | None,
        spec: PreparedRequirementSpec,
        *,
        priority: str | None = None,
    ) -> ResourceRequirement:
        proposed = (
            self._session.get(Resource, spec.proposed_resource_id)
            if spec.proposed_resource_id
            else None
        )
        if spec.proposed_resource_id and proposed is None:
            raise KeyError(
                f"Ressource proposée {spec.proposed_resource_id} introuvable pour la ligne {spec.source_request_line_id}."
            )

        if requirement is None:
            identifier = self._segments.create(
                {
                    "NoDemande": _text(request.legacy_demand_number) or request.id,
                    "NumeroProjet": project.number,
                    "Technicien": proposed.name if proposed is not None else None,
                    "DateDebut": spec.start_date,
                    "DateFin": spec.end_date,
                    "HeuresPrevues": spec.planned_hours,
                    "JoursActifsCibles": spec.desired_active_days,
                    "Statut": "Planifié" if proposed is not None else "À assigner",
                    "Description": spec.description,
                    "SourceEffortID": spec.source_effort_id,
                    "ClasseRessourceRequise": spec.required_resource_class,
                    "CompetenceRequise": spec.required_competency,
                    "RequiredCompetencyIDs": spec.competency_ids,
                    "SourceRequestLineID": spec.source_request_line_id,
                    "TypePlanification": "Flexible",
                    "Priorite": (
                        priority
                        if priority is not None
                        else request.priority or "Normale"
                    ),
                    "HorsHoraireAutorise": False,
                    "OrigineSegment": ORIGIN_REQUEST,
                    "Confirmation": spec.confirmation,
                    "ConfirmationOverride": False,
                }
            )
            requirement = self._session.scalar(
                select(ResourceRequirement).where(
                    ResourceRequirement.legacy_segment_id == identifier
                )
            )
            if requirement is None:
                raise RuntimeError(
                    f"Le besoin créé pour la ligne {spec.source_request_line_id} est introuvable."
                )
        elif (
            proposed is not None
            and requirement.assigned_resource_id is None
        ):
            requirement.assigned_resource_id = proposed.id

        if requirement.assigned_resource_id:
            if requirement.status != "Terminé":
                requirement.status = "Planifié"
        elif requirement.status not in {"Terminé", "Annulé"}:
            requirement.status = "À assigner"

        requirement.project_id = project.id
        requirement.workforce_request_id = request.id
        requirement.source_request_line_id = spec.source_request_line_id
        requirement.start_date = spec.start_date
        requirement.end_date = spec.end_date
        requirement.planned_hours = spec.planned_hours
        requirement.desired_active_days = spec.desired_active_days
        requirement.description = spec.description
        requirement.source_effort_id = spec.source_effort_id
        requirement.required_resource_class = spec.required_resource_class
        requirement.required_competency = spec.required_competency
        requirement.required_competency_id = (
            spec.competency_ids[0] if len(spec.competency_ids) == 1 else None
        )
        requirement.priority = (
            priority
            if priority is not None
            else request.priority or "Normale"
        )
        requirement.origin = ORIGIN_REQUEST
        source_line = (
            self._session.get(RequestLine, spec.source_request_line_id)
            if spec.source_request_line_id
            else None
        )
        self._capture_approved_contact_context(
            request,
            requirement,
            source_line,
        )
        if not requirement.confirmation_overridden:
            requirement.confirmation = spec.confirmation
        self._replace_requirement_competencies(requirement, spec.competency_ids)
        source_period = (
            self._session.get(WorkforceRequestPeriod, spec.source_period_id)
            if spec.source_period_id
            else None
        )
        self._update_period_link(requirement, source_period)
        return requirement

    def _sync_request_lines(
        self,
        request: WorkforceRequest,
        periods: list[WorkforceRequestPeriod],
    ) -> None:
        project = self._session.get(Project, request.project_id)
        if project is None:
            raise KeyError(f"Projet {request.project_id} introuvable")
        lines = self._active_lines(request.id)
        if not lines:
            raise ValueError("Une demande multi-lignes doit conserver au moins une ligne active.")

        current = self._active_requirements(request.id)
        prepared = self._plan_preparer.prepare(request, current=current)
        specs = list(prepared.specs)
        unresolved = prepared.unresolved_groups
        self._plan_preparer.assert_locked_compatible(
            request,
            current,
            specs,
            target_project_id=request.project_id,
        )
        current_by_key = self._current_requirement_keys(current)

        keep: dict[tuple[str, ...], ResourceRequirement | None] = {}
        desired_by_key = {spec.key: spec for spec in specs}
        obsolete: list[ResourceRequirement] = []

        for key, rows in current_by_key.items():
            spec = desired_by_key.get(key)
            if spec is None:
                obsolete.extend(rows)
                continue
            keep[key] = rows[0]
            obsolete.extend(rows[1:])

        for requirement in obsolete:
            requirement.status = "Annulé"

        materialized: list[ResourceRequirement] = []
        for spec in specs:
            materialized.append(
                self._apply_line_spec(
                    request,
                    project,
                    keep.get(spec.key),
                    spec,
                )
            )

        emergency = self._emergency_materialization(request)
        self._session.add(
            WorkforceRequestHistory(
                workforce_request_id=request.id,
                action=(
                    "Synchronisation lignes urgente"
                    if emergency
                    else "Synchronisation lignes"
                ),
                status=request.status,
                comment=(
                    f"{len(materialized)} besoin(s) matérialisé(s) depuis "
                    f"{len(lines)} ligne(s)"
                    + (
                        " par dérogation urgente"
                        if emergency
                        else ""
                    )
                    + f"; {len(obsolete)} besoin(s) remplacé(s)/annulé(s); "
                    f"{unresolved} groupe(s) alternatif(s) non résolu(s)."
                ),
                actor_name=(
                    request.emergency_override_by_name
                    if emergency
                    else request.approved_by_name
                ),
                details=self._approved_context_details(request, materialized),
                occurred_at=utc_now(),
            )
        )
        self._session.flush()

    def sync_operational_choices(self, demand_number: str) -> None:
        """Resync active planning from approved authorization, never from candidate data."""

        request = self._guarded_materialization_request(demand_number)
        current = self._active_requirements(request.id)
        prepared = self._plan_preparer.prepare_active(
            request,
            current=current,
        )
        self._plan_preparer.assert_locked_compatible(
            request,
            current,
            prepared.specs,
            target_project_id=prepared.project_id,
        )
        matches, obsolete = self._plan_preparer.match_current(
            request,
            current,
            prepared.specs,
        )
        approved_project_id = _text(prepared.project_id)
        if not approved_project_id:
            raise ValueError(
                "La révision approuvée active ne contient pas de projet."
            )
        project = self._session.get(Project, approved_project_id)
        if project is None:
            raise KeyError(f"Projet {approved_project_id} introuvable")

        self._responsibility_revision_id = prepared.approval_revision_id
        self._restore_approved_responsibility_only = True

        for requirement in obsolete:
            requirement.status = "Annulé"

        materialized: list[ResourceRequirement] = []
        for match in matches:
            requirement = self._apply_line_spec(
                request,
                project,
                match.requirement,
                match.spec,
                priority=prepared.priority,
            )
            requirement.approval_revision_id = prepared.approval_revision_id
            requirement.approved_entry_key = match.spec.approved_entry_key
            requirement.approval_reference_status = APPROVAL_REFERENCE_CAPTURED
            materialized.append(requirement)

        self._session.add(
            WorkforceRequestHistory(
                workforce_request_id=request.id,
                action="Synchronisation choix opérationnels",
                status=request.status,
                comment=(
                    f"{len(materialized)} besoin(s) actifs synchronisés contre la "
                    f"révision {prepared.approval_revision_id}; "
                    f"version opérationnelle {prepared.operational_version}."
                ),
                actor_name=request.approved_by_name,
                occurred_at=utc_now(),
            )
        )
        self._session.flush()
        from .asset_plan import SqlAssetPlanSynchronizer
        SqlAssetPlanSynchronizer(self._session).from_active_revision(request)

    def cancel_materialized(self, demand_number: str) -> None:
        request = self._request(demand_number)
        current = self._active_requirements(request.id)
        segment_assets = segment_asset_dependencies(
            self._session,
            tuple(row.id for row in current),
        )
        if segment_assets:
            raise ApplicationConflictError(
                "La demande contient une réservation d'actif portée par un segment. "
                "Utilise la demande d'annulation coordonnée pour la libérer explicitement.",
                code="active_operational_decisions",
                context={
                    "asset_allocation_ids": [
                        row.allocation_id for row in segment_assets
                    ],
                },
            )
        from .asset_plan import SqlAssetPlanSynchronizer
        SqlAssetPlanSynchronizer(self._session).cancel(request)
        locked = self._locked_shifts({row.id for row in current})
        protected = [
            row
            for row in current
            if row.status != "Terminé" and locked.get(row.id)
        ]
        if protected:
            raise ValueError(
                "La demande contient des quarts verrouillés. Libère ou supprime ces "
                "décisions manuelles avant d'annuler la demande."
            )
        for requirement in current:
            if requirement.status != "Terminé":
                requirement.status = "Annulé"
        self._session.flush()

    def sync_approved(
        self,
        demand_number: str,
        *,
        approved_request_version: int | None = None,
    ) -> None:
        self._approved_request_version_override = (
            int(approved_request_version)
            if approved_request_version is not None
            else None
        )
        request = self._guarded_materialization_request(demand_number)
        periods = self._active_periods(request.id)
        current_before = self._active_requirements(request.id)
        prepared = self._plan_preparer.prepare(
            request,
            current=current_before,
        )
        self._plan_preparer.assert_locked_compatible(
            request,
            current_before,
            prepared.specs,
            target_project_id=request.project_id,
        )
        if not bool(request.line_mode) and not periods:
            self._legacy.prevalidate_approved(demand_number)
        emergency = self._emergency_materialization(request)
        revision = (
            None
            if emergency
            else self._approval_revisions.create_revision(
                request,
                request_version=self._approved_request_version(request),
            )
        )
        self._responsibility_revision_id = revision.id if revision is not None else None
        self._restore_approved_responsibility_only = False

        if bool(request.line_mode):
            self._sync_request_lines(request, periods)
        elif not periods:
            self._legacy.sync_approved(demand_number)
            legacy_line = self._session.get(RequestLine, request.id)
            materialized = self._active_requirements(request.id)
            legacy_competency_ids = (
                prepared.specs[0].competency_ids
                if prepared.specs
                else ()
            )
            for requirement in materialized:
                requirement.required_competency_id = (
                    legacy_competency_ids[0]
                    if len(legacy_competency_ids) == 1
                    else None
                )
                self._replace_requirement_competencies(
                    requirement,
                    legacy_competency_ids,
                )
                self._capture_approved_contact_context(
                    request,
                    requirement,
                    legacy_line,
                )
            self._session.add(
                WorkforceRequestHistory(
                    workforce_request_id=request.id,
                    action="Capture contexte approuvé",
                    status=request.status,
                    comment=(
                        f"Contexte approuvé capturé sur {len(materialized)} besoin(s) legacy."
                    ),
                    details=self._approved_context_details(request, materialized),
                    actor_name=request.approved_by_name,
                    occurred_at=utc_now(),
                )
            )
            self._session.flush()
        else:
            self._sync_legacy_periods(request, periods)

        if revision is not None:
            from .asset_plan import SqlAssetPlanSynchronizer
            SqlAssetPlanSynchronizer(self._session).sync(
                request, self._approval_revisions.candidate_envelope(request), revision
            )
            self._approval_revisions.bind_materialized_requirements(
                request,
                revision,
            )
            self._approval_revisions.activate_revision(request, revision)
            self._session.add(
                WorkforceRequestHistory(
                    workforce_request_id=request.id,
                    action="Capture révision approuvée",
                    status=request.status,
                    comment="Autorisation approuvée immuable capturée et activée.",
                    details=json.dumps(
                        {
                            "approval_revision_id": revision.id,
                            "request_version": revision.request_version,
                            "authorization_fingerprint": (
                                revision.authorization_fingerprint
                            ),
                            "payload_format_version": (
                                revision.payload_format_version
                            ),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    actor_name=request.approved_by_name,
                    occurred_at=utc_now(),
                )
            )
            self._session.flush()
