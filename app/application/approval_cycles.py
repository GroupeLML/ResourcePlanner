from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence

from ..domain.approval_cycles import (
    APPROVAL_CYCLE_INIT_LEGACY_EXPLICIT,
    APPROVAL_CYCLE_INIT_SUBMISSION,
    APPROVAL_CYCLE_STATE_OPEN,
    LEGACY_APPROVAL_CYCLE_ACTIVE,
    LEGACY_APPROVAL_CYCLE_APPROVED_HISTORICAL,
    LEGACY_APPROVAL_CYCLE_NONE,
    LEGACY_APPROVAL_CYCLE_SUBMITTED_REQUIRES_INITIALIZATION,
    ApprovalSubjectRoutingEntry,
    approval_subject_fingerprint,
)
from ..domain.approval_routing import ROUTING_SOURCE_ASSET_TYPE
from .approval_scopes import ApprovalScopeService
from .errors import (
    ApplicationConflictError,
    ApplicationNotFoundError,
    ApplicationValidationError,
    call_application_port,
)


@dataclass(frozen=True, slots=True)
class ApprovalCycleRequestRecord:
    id: str
    number: str
    status: str
    aggregate_version: int
    project_id: str
    priority: str | None
    site_client: str | None
    location: str | None
    line_mode: bool
    approved_at: object | None


@dataclass(frozen=True, slots=True)
class ApprovalCycleSubjectRecord:
    request_id: str
    project_id: str
    priority: str | None
    site_client: str | None
    location: str | None
    line_mode: bool
    authorization_entries: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True)
class ApprovalCycleRoutingInput:
    request_line_id: str
    task_catalog_item_id: str | None
    approval_scope_ids: tuple[str, ...]
    proposed_resource_id: str | None
    line_kind: str = "WORKFORCE"
    period_proposed_resources: tuple[tuple[str, str | None], ...] = ()
    asset_type_id: str | None = None
    proposed_asset_id: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovalApproverSnapshot:
    app_user_id: str
    sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ApprovalRequirementSnapshot:
    request_line_id: str
    task_catalog_item_id: str | None
    approval_scope_id: str
    proposed_resource_id: str | None
    routing_sources: tuple[str, ...]
    approvers: tuple[ApprovalApproverSnapshot, ...]
    asset_type_id: str | None = None
    proposed_asset_id: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovalRequirementRecord:
    id: str
    request_line_id: str
    task_catalog_item_id: str | None
    approval_scope_id: str | None
    proposed_resource_id: str | None
    routing_sources: tuple[str, ...]
    approvers: tuple[ApprovalApproverSnapshot, ...]
    asset_type_id: str | None = None
    proposed_asset_id: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovalCycleRecord:
    id: str
    workforce_request_id: str
    submitted_request_version: int
    state: str
    subject_fingerprint: str
    initialization_reason: str
    submitted_at: object
    invalidated_at: object | None
    invalidation_reason: str | None
    completed_at: object | None
    approved_revision_id: str | None
    requirements: tuple[ApprovalRequirementRecord, ...]


class ApprovalCycleRepositoryPort(Protocol):
    def get_request(self, request_id: str) -> ApprovalCycleRequestRecord | None: ...
    def list_active_line_ids(self, request_id: str) -> tuple[str, ...]: ...
    def read_subject(self, request_id: str) -> ApprovalCycleSubjectRecord: ...
    def read_routing_inputs(
        self,
        request_id: str,
    ) -> tuple[ApprovalCycleRoutingInput, ...]: ...
    def get_active_cycle(self, request_id: str) -> ApprovalCycleRecord | None: ...
    def get_latest_cycle(self, request_id: str) -> ApprovalCycleRecord | None: ...
    def has_any_cycle(self, request_id: str) -> bool: ...
    def create_cycle(
        self,
        *,
        request_id: str,
        submitted_request_version: int,
        expected_version: int,
        subject_fingerprint: str,
        initialization_reason: str,
        requirements: Sequence[ApprovalRequirementSnapshot],
    ) -> ApprovalCycleRecord: ...
    def invalidate_cycle(
        self,
        *,
        cycle_id: str,
        expected_version: int,
        reason: str,
    ) -> ApprovalCycleRecord: ...


class ResourceApprovalAuthorityPort(Protocol):
    """Future stable Resource -> AppUser authority source prepared by 276B."""

    def list_approver_user_ids(self, resource_id: str) -> tuple[str, ...]: ...


def _required(value: object, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ApplicationValidationError(
            f"{field} est requis.",
            code="approval_cycle_field_required",
            context={"field": field},
        )
    return normalized


class ApprovalCycleService:
    def __init__(
        self,
        repository: ApprovalCycleRepositoryPort,
        approval_scopes: ApprovalScopeService,
        *,
        resource_authority: ResourceApprovalAuthorityPort | None = None,
    ) -> None:
        self._repository = repository
        self._approval_scopes = approval_scopes
        self._resource_authority = resource_authority

    def _request(self, request_id: str) -> ApprovalCycleRequestRecord:
        identifier = _required(request_id, "workforce_request_id")
        record = call_application_port(
            lambda: self._repository.get_request(identifier),
            code_prefix="approval_cycle_request_read",
            context={"workforce_request_id": identifier},
        )
        if record is None:
            raise ApplicationNotFoundError(
                "Demande introuvable.",
                code="approval_cycle_request_not_found",
                context={"workforce_request_id": identifier},
            )
        return record

    def get_request(self, request_id: str) -> ApprovalCycleRequestRecord | None:
        identifier = _required(request_id, "workforce_request_id")
        return call_application_port(
            lambda: self._repository.get_request(identifier),
            code_prefix="approval_cycle_request_read",
            context={"workforce_request_id": identifier},
        )

    def get_active_cycle(self, request_id: str) -> ApprovalCycleRecord | None:
        identifier = _required(request_id, "workforce_request_id")
        return call_application_port(
            lambda: self._repository.get_active_cycle(identifier),
            code_prefix="approval_cycle_read",
            context={"workforce_request_id": identifier},
        )

    def get_latest_cycle(self, request_id: str) -> ApprovalCycleRecord | None:
        identifier = _required(request_id, "workforce_request_id")
        return call_application_port(
            lambda: self._repository.get_latest_cycle(identifier),
            code_prefix="approval_cycle_read",
            context={"workforce_request_id": identifier},
        )

    def _fingerprint(
        self,
        *,
        subject: ApprovalCycleSubjectRecord,
        routing_entries: Sequence[ApprovalSubjectRoutingEntry],
    ) -> str:
        return approval_subject_fingerprint(
            request_id=subject.request_id,
            project_id=subject.project_id,
            priority=subject.priority,
            site_client=subject.site_client,
            location=subject.location,
            line_mode=subject.line_mode,
            authorization_entries=subject.authorization_entries,
            routing_entries=routing_entries,
        )

    def _resource_approvers(
        self,
        *,
        line_id: str,
        proposed_resource_id: str | None,
    ) -> tuple[str, ...]:
        if self._resource_authority is None or proposed_resource_id is None:
            return ()
        return call_application_port(
            lambda: self._resource_authority.list_approver_user_ids(
                proposed_resource_id
            ),
            code_prefix="approval_cycle_resource_authority_read",
            context={
                "request_line_id": line_id,
                "proposed_resource_id": proposed_resource_id,
            },
        )

    @staticmethod
    def _assert_resolved_routing(
        resolved: object,
        *,
        routing: ApprovalCycleRoutingInput,
        proposed_resource_id: str | None,
        period_key: str | None,
    ) -> None:
        resolution = resolved.resolution
        if (
            not resolution.blocked
            and resolution.approval_scope_id
            and resolution.eligible_approvers
        ):
            return
        resolved_scope = next(
            (
                scope
                for scope in resolved.approval_scope_candidates
                if scope.id == resolution.approval_scope_id
            ),
            None,
        )
        raise ApplicationValidationError(
            "Le routage d’approbation de la ligne est incomplet ou ambigu.",
            code="approval_cycle_routing_blocked",
            context={
                "request_line_id": resolved.request_line_id,
                "request_line_position": resolved.line_position,
                "period_key": period_key,
                "task_catalog_item_id": resolved.task_catalog_item_id,
                "task_code": resolved.task_code,
                "task_label": resolved.task_label,
                "required_resource_class": resolved.required_resource_class,
                "effective_resource_class": resolved.effective_resource_class,
                "routing_sources": list(resolved.routing_sources),
                "approval_scope": (
                    {
                        "id": resolved_scope.id,
                        "code": resolved_scope.code,
                        "label": resolved_scope.label,
                        "active": resolved_scope.active,
                    }
                    if resolved_scope is not None
                    else None
                ),
                "approval_scope_candidates": [
                    {
                        "id": scope.id,
                        "code": scope.code,
                        "label": scope.label,
                        "active": scope.active,
                    }
                    for scope in resolved.approval_scope_candidates
                ],
                "suggested_scope_code": resolved.suggested_scope_code,
                "proposed_resource_id": proposed_resource_id,
                "period_proposed_resources": [
                    [key, resource_id]
                    for key, resource_id in routing.period_proposed_resources
                ],
                "line_kind": resolved.line_kind,
                "asset_type_id": resolved.asset_type_id,
                "asset_type_code": resolved.asset_type_code,
                "asset_type_label": resolved.asset_type_label,
                "proposed_asset_id": resolved.proposed_asset_id,
                "proposed_asset_code": resolved.proposed_asset_code,
                "proposed_asset_label": resolved.proposed_asset_label,
                "diagnostics": list(resolution.diagnostics),
            },
        )

    def _resolve_line_routing(
        self,
        line_id: str,
        routing: ApprovalCycleRoutingInput,
    ) -> tuple[object, tuple[ApprovalApproverSnapshot, ...], tuple[str, ...], str | None]:
        variants = (
            routing.period_proposed_resources
            if routing.period_proposed_resources
            else (("", routing.proposed_resource_id),)
        )
        resolved_variants: list[object] = []
        approver_sources: dict[str, set[str]] = {}
        routing_sources: set[str] = set()
        for period_key, proposed_resource_id in variants:
            resource_approvers = self._resource_approvers(
                line_id=line_id,
                proposed_resource_id=proposed_resource_id,
            )
            resolved = self._approval_scopes.resolve_request_line(
                line_id,
                resource_approver_user_ids=resource_approvers,
                proposed_resource_id_override=proposed_resource_id,
            )
            self._assert_resolved_routing(
                resolved,
                routing=routing,
                proposed_resource_id=proposed_resource_id,
                period_key=period_key or None,
            )
            resolved_variants.append(resolved)
            routing_sources.update(resolved.routing_sources)
            for candidate in resolved.resolution.eligible_approvers:
                approver_sources.setdefault(candidate.user_id, set()).update(
                    candidate.sources
                )

        scope_ids = {
            resolved.resolution.approval_scope_id
            for resolved in resolved_variants
        }
        if len(scope_ids) != 1:
            raise ApplicationValidationError(
                "Les propositions effectives des périodes conduisent à des périmètres d’approbation incompatibles.",
                code="approval_cycle_period_routing_incompatible",
                context={
                    "request_line_id": line_id,
                    "approval_scope_ids": sorted(
                        str(value) for value in scope_ids if value
                    ),
                    "period_proposed_resources": [
                        [period_key, resource_id]
                        for period_key, resource_id in routing.period_proposed_resources
                    ],
                },
            )

        resolved = resolved_variants[0]
        approvers = tuple(
            ApprovalApproverSnapshot(
                app_user_id=user_id,
                sources=tuple(sorted(sources)),
            )
            for user_id, sources in sorted(approver_sources.items())
        )
        source_kinds_set = {
            source
            for approver in approvers
            for source in approver.sources
        }
        if resolved.task_catalog_item_id is None:
            source_kinds_set.update(routing_sources)
        if routing.line_kind == "ASSET":
            source_kinds_set.add(ROUTING_SOURCE_ASSET_TYPE)
        source_kinds = tuple(sorted(source_kinds_set))

        if routing.period_proposed_resources:
            resources = {resource_id for _, resource_id in variants}
            snapshot_resource_id = (
                next(iter(resources)) if len(resources) == 1 else None
            )
        else:
            snapshot_resource_id = routing.proposed_resource_id
        return resolved, approvers, source_kinds, snapshot_resource_id

    def initialize_cycle(
        self,
        request_id: str,
        *,
        expected_version: int,
        initialization_reason: str = APPROVAL_CYCLE_INIT_SUBMISSION,
    ) -> ApprovalCycleRecord:
        request = self._request(request_id)
        if request.status != "Soumise":
            raise ApplicationValidationError(
                "Un cycle d'approbation ne peut être initialisé que pour une demande soumise.",
                code="approval_cycle_request_not_submitted",
                context={
                    "workforce_request_id": request.id,
                    "status": request.status,
                },
            )
        if initialization_reason not in {
            APPROVAL_CYCLE_INIT_SUBMISSION,
            APPROVAL_CYCLE_INIT_LEGACY_EXPLICIT,
        }:
            raise ApplicationValidationError(
                "La provenance d'initialisation du cycle est invalide.",
                code="approval_cycle_initialization_reason_invalid",
            )

        existing = self.get_active_cycle(request.id)
        if existing is not None:
            raise ApplicationConflictError(
                "Un cycle d'approbation actif existe déjà pour cette demande.",
                code="approval_cycle_already_active",
                context={
                    "workforce_request_id": request.id,
                    "approval_cycle_id": existing.id,
                },
            )

        line_ids = call_application_port(
            lambda: self._repository.list_active_line_ids(request.id),
            code_prefix="approval_cycle_lines_read",
            context={"workforce_request_id": request.id},
        )
        if not line_ids:
            raise ApplicationValidationError(
                "La demande soumise ne contient aucune ligne active à approuver.",
                code="approval_cycle_no_active_line",
                context={"workforce_request_id": request.id},
            )

        routing_inputs = call_application_port(
            lambda: self._repository.read_routing_inputs(request.id),
            code_prefix="approval_cycle_routing_read",
            context={"workforce_request_id": request.id},
        )
        routing_by_line = {
            row.request_line_id: row
            for row in routing_inputs
        }

        requirements: list[ApprovalRequirementSnapshot] = []
        fingerprint_routing: list[ApprovalSubjectRoutingEntry] = []
        for line_id in sorted(line_ids):
            routing = routing_by_line.get(line_id)
            if routing is None:
                raise ApplicationValidationError(
                    "Les entrées de routage de la ligne sont incomplètes.",
                    code="approval_cycle_routing_incomplete",
                    context={"request_line_id": line_id},
                )

            resolved, approvers, source_kinds, snapshot_resource_id = (
                self._resolve_line_routing(line_id, routing)
            )
            requirement = ApprovalRequirementSnapshot(
                request_line_id=line_id,
                task_catalog_item_id=resolved.task_catalog_item_id,
                approval_scope_id=resolved.resolution.approval_scope_id,
                proposed_resource_id=snapshot_resource_id,
                routing_sources=source_kinds,
                approvers=approvers,
                asset_type_id=routing.asset_type_id,
                proposed_asset_id=routing.proposed_asset_id,
            )
            requirements.append(requirement)
            fingerprint_routing.append(
                ApprovalSubjectRoutingEntry(
                    request_line_id=line_id,
                    task_catalog_item_id=resolved.task_catalog_item_id,
                    approval_scope_ids=(
                        resolved.resolution.approval_scope_id,
                    ),
                    source_kinds=source_kinds,
                    proposed_resource_id=snapshot_resource_id,
                    proposed_asset_id=routing.proposed_asset_id,
                    period_proposed_resources=routing.period_proposed_resources,
                )
            )

        subject = call_application_port(
            lambda: self._repository.read_subject(request.id),
            code_prefix="approval_cycle_subject_read",
            context={"workforce_request_id": request.id},
        )
        fingerprint = self._fingerprint(
            subject=subject,
            routing_entries=fingerprint_routing,
        )
        return call_application_port(
            lambda: self._repository.create_cycle(
                request_id=request.id,
                submitted_request_version=request.aggregate_version,
                expected_version=int(expected_version),
                subject_fingerprint=fingerprint,
                initialization_reason=initialization_reason,
                requirements=requirements,
            ),
            code_prefix="approval_cycle_create",
            context={"workforce_request_id": request.id},
        )

    def _current_fingerprint(
        self,
        cycle: ApprovalCycleRecord,
    ) -> str:
        subject = call_application_port(
            lambda: self._repository.read_subject(cycle.workforce_request_id),
            code_prefix="approval_cycle_subject_read",
            context={
                "workforce_request_id": cycle.workforce_request_id,
                "approval_cycle_id": cycle.id,
            },
        )
        current_routing = call_application_port(
            lambda: self._repository.read_routing_inputs(
                cycle.workforce_request_id
            ),
            code_prefix="approval_cycle_routing_read",
            context={
                "workforce_request_id": cycle.workforce_request_id,
                "approval_cycle_id": cycle.id,
            },
        )
        submitted_by_line = {
            row.request_line_id: row
            for row in cycle.requirements
        }
        routing_entries: list[ApprovalSubjectRoutingEntry] = []
        for row in current_routing:
            submitted = submitted_by_line.get(row.request_line_id)
            source_kinds = (
                submitted.routing_sources
                if submitted is not None
                else ()
            )
            routing_entries.append(
                ApprovalSubjectRoutingEntry(
                    request_line_id=row.request_line_id,
                    task_catalog_item_id=row.task_catalog_item_id,
                    approval_scope_ids=(
                        (submitted.approval_scope_id,)
                        if submitted is not None
                        and submitted.approval_scope_id is not None
                        else row.approval_scope_ids
                    ),
                    source_kinds=source_kinds,
                    proposed_resource_id=row.proposed_resource_id,
                    proposed_asset_id=row.proposed_asset_id,
                    period_proposed_resources=row.period_proposed_resources,
                )
            )
        return self._fingerprint(
            subject=subject,
            routing_entries=routing_entries,
        )

    def current_subject_fingerprint(self, request_id: str) -> str:
        cycle = self.get_active_cycle(request_id)
        if cycle is None:
            raise ApplicationNotFoundError(
                "Aucun cycle d'approbation actif n'existe pour cette demande.",
                code="approval_cycle_not_found",
                context={"workforce_request_id": request_id},
            )
        return self._current_fingerprint(cycle)

    def validate_active_cycle(
        self,
        request_id: str,
    ) -> ApprovalCycleRecord:
        cycle = self.get_active_cycle(request_id)
        if cycle is None:
            raise ApplicationNotFoundError(
                "Aucun cycle d'approbation actif n'existe pour cette demande.",
                code="approval_cycle_not_found",
                context={"workforce_request_id": request_id},
            )
        current = self._current_fingerprint(cycle)
        if current != cycle.subject_fingerprint:
            raise ApplicationConflictError(
                "Le sujet soumis à approbation a changé depuis la création du cycle.",
                code="approval_cycle_subject_changed",
                context={
                    "approval_cycle_id": cycle.id,
                    "submitted_fingerprint": cycle.subject_fingerprint,
                    "current_fingerprint": current,
                },
            )
        return cycle

    def invalidate_if_subject_changed(
        self,
        request_id: str,
        *,
        expected_version: int,
        reason: str = "SUBJECT_CHANGED",
    ) -> ApprovalCycleRecord | None:
        cycle = self.get_active_cycle(request_id)
        if cycle is None:
            return None
        current = self._current_fingerprint(cycle)
        if current == cycle.subject_fingerprint:
            return cycle
        return call_application_port(
            lambda: self._repository.invalidate_cycle(
                cycle_id=cycle.id,
                expected_version=int(expected_version),
                reason=_required(reason, "invalidation_reason"),
            ),
            code_prefix="approval_cycle_invalidate",
            context={
                "workforce_request_id": cycle.workforce_request_id,
                "approval_cycle_id": cycle.id,
            },
        )

    def invalidate_cycle(
        self,
        request_id: str,
        *,
        expected_version: int,
        reason: str,
    ) -> ApprovalCycleRecord | None:
        cycle = self.get_active_cycle(request_id)
        if cycle is None:
            return None
        return call_application_port(
            lambda: self._repository.invalidate_cycle(
                cycle_id=cycle.id,
                expected_version=int(expected_version),
                reason=_required(reason, "invalidation_reason"),
            ),
            code_prefix="approval_cycle_invalidate",
            context={
                "workforce_request_id": cycle.workforce_request_id,
                "approval_cycle_id": cycle.id,
            },
        )

    def legacy_compatibility_state(self, request_id: str) -> str:
        request = self._request(request_id)
        active = self.get_active_cycle(request.id)
        if active is not None and active.state == APPROVAL_CYCLE_STATE_OPEN:
            return LEGACY_APPROVAL_CYCLE_ACTIVE
        if request.status == "Soumise":
            return LEGACY_APPROVAL_CYCLE_SUBMITTED_REQUIRES_INITIALIZATION
        if request.approved_at is not None:
            return LEGACY_APPROVAL_CYCLE_APPROVED_HISTORICAL
        return LEGACY_APPROVAL_CYCLE_NONE
