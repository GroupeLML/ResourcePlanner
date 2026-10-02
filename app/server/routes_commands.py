from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, Header, Query, status
from fastapi.responses import JSONResponse
from ..application import (
    AllocationDropEvaluateCommand,
    AllocationDuplicateCommand,
    AllocationExtendMoveCommand,
    AllocationSplitCommand,
    AllocationWindowExtensionProposalCommand,
    ApprovalVoteCommand,
    ApplicationFacade,
    AvailabilityRuleCreateCommand,
    CompetencyCatalogService,
    AvailabilityRuleUpdateCommand,
    DemandAlternativeSelectCommand,
    DemandOperationalConfirmationCommand,
    DemandApproveCommand,
    DemandCancelCommand,
    DemandCancellationAcceptCommand,
    DemandCancellationRejectCommand,
    DemandCancellationRequestCommand,
    DemandCorrectionCommand,
    DemandCreateCommand,
    DemandLineInput,
    DemandEmergencyOverrideCommand,
    DemandPeriodInput,
    DemandPeriodsReplaceCommand,
    DemandSubmitCommand,
    DemandUpdateCommand,
    IdempotentCommandExecutor,
    ManualAllocationCreateCommand,
    ManualAllocationDeleteCommand,
    ManualAllocationMoveCommand,
    ManualAllocationReleaseCommand,
    ManualAllocationUpdateCommand,
    PlanningRebuildCommand,
    QuickShiftCreateCommand,
    ResourceCreateCommand,
    ResourceUpdateCommand,
    SegmentAssignCommand,
    SegmentCancelCommand,
    SegmentCreateCommand,
    SegmentUpdateCommand,
    WorkPackageCreateCommand,
    WorkPackageUpdateCommand,
)
from ..application.work_package_weekly_load import (
    WeeklyLoadValue,
    WorkPackageWeeklyLoadReplaceCommand,
)
from .schemas import (
    AllocationDropEvaluateRequest,
    AllocationDuplicateRequest,
    AllocationExtendMoveRequest,
    AllocationMoveRequest,
    AllocationSplitRequest,
    AllocationWindowExtensionProposalRequest,
    AtomicAllocationBaseRequest,
    AvailabilityRuleCreateRequest,
    AvailabilityRuleUpdateRequest,
    DemandAlternativeSelectionRequest,
    DemandApprovalRequest,
    DemandApprovalVoteRequest,
    DemandCancellationAcceptRequest,
    DemandCancellationRejectRequest,
    DemandCancellationRequest,
    DemandOperationalConfirmationRequest,
    DemandCreateRequest,
    DemandLineRequest,
    DemandPeriodsReplaceRequest,
    DemandUpdateRequest,
    DemandWorkflowOptionalCommentRequest,
    DemandWorkflowRequiredCommentRequest,
    DemandWorkflowVersionRequest,
    ManualAllocationRequest,
    OptionalCommentRequest,
    QuickShiftRequest,
    RequiredCommentRequest,
    ResourceCreateRequest,
    ResourceUpdateRequest,
    SegmentAssignRequest,
    SegmentCreateRequest,
    SegmentUpdateRequest,
    WorkPackageCreateRequest,
    WorkPackageUpdateRequest,
    WorkPackageWeeklyLoadReplaceRequest,
)


FacadeProvider = Callable[..., Any]
IdempotencyProvider = Callable[..., Any]
CompetencyProvider = Callable[..., Any]


def _payload(result: Any) -> dict[str, Any]:
    return result.to_dict()


def _json_body(body: Any) -> dict[str, Any]:
    return body.model_dump(mode="json")


def _demand_line_inputs(
    lines: list[DemandLineRequest] | None,
    competencies: CompetencyCatalogService,
) -> tuple[DemandLineInput, ...] | None:
    if lines is None:
        return None
    result: list[DemandLineInput] = []
    for index, line in enumerate(lines):
        competency_ids = tuple(line.required_competency_ids or ())
        values = line.model_dump(exclude={"required_competency_ids"})
        values["position"] = (
            line.position if line.position is not None else index
        )
        values["line_id"] = values.pop("id", None)
        values["required_competency_ids"] = competency_ids
        values["required_competencies"] = (
            competencies.snapshot_text(competency_ids)
            if competency_ids
            else None
        )
        result.append(DemandLineInput(**values))
    return tuple(result)


def _segment_command_values(
    body: SegmentCreateRequest | SegmentUpdateRequest,
    competencies: CompetencyCatalogService,
) -> tuple[dict[str, Any], bool, str | None]:
    values = body.model_dump(exclude_unset=isinstance(body, SegmentUpdateRequest))
    if "source_effort_id" in values:
        values["source_effort_row"] = values.pop("source_effort_id")
    competency_supplied = "required_competency_id" in body.model_fields_set
    competency_id = values.pop("required_competency_id", None)
    if competency_supplied:
        values["required_competency"] = (
            competencies.snapshot_text((competency_id,))
            if competency_id
            else None
        )
    return values, competency_supplied, competency_id


def build_command_router(
    facade_dependency: FacadeProvider,
    idempotency_dependency: IdempotencyProvider,
    competency_dependency: CompetencyProvider,
    stable_idempotency_dependency: IdempotencyProvider | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["commands"])
    stable_idempotency = stable_idempotency_dependency or idempotency_dependency

    @router.post("/resources", status_code=status.HTTP_201_CREATED)
    def create_resource(
        body: ResourceCreateRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        facade: ApplicationFacade = Depends(facade_dependency),
        idempotency: IdempotentCommandExecutor = Depends(idempotency_dependency),
        competencies: CompetencyCatalogService = Depends(competency_dependency),
    ) -> dict[str, Any]:
        selection_supplied = "competency_ids" in body.model_fields_set
        competency_ids = tuple(body.competency_ids or ())
        values = body.model_dump(exclude={"competency_ids"})
        if selection_supplied:
            values["competencies"] = competencies.snapshot_text(competency_ids)

        def action() -> dict[str, Any]:
            result = facade.create_resource(ResourceCreateCommand(**values))
            if selection_supplied:
                competencies.assign_resource(result.resource_id, competency_ids)
            return _payload(result)

        return idempotency.execute(
            scope="resource.create",
            key=idempotency_key,
            request_payload=_json_body(body),
            action=action,
        )

    @router.patch("/resources/{resource_id}")
    def update_resource(
        resource_id: str,
        body: ResourceUpdateRequest,
        facade: ApplicationFacade = Depends(facade_dependency),
        competencies: CompetencyCatalogService = Depends(competency_dependency),
    ) -> dict[str, Any]:
        selection_supplied = "competency_ids" in body.model_fields_set
        competency_ids = tuple(body.competency_ids or ())
        values = body.model_dump(exclude_unset=True, exclude={"competency_ids"})
        if selection_supplied:
            values["competencies"] = competencies.snapshot_text(competency_ids)
        result = facade.update_resource(
            ResourceUpdateCommand(resource_id=resource_id, **values)
        )
        if selection_supplied:
            competencies.assign_resource(resource_id, competency_ids)
        return _payload(result)

    @router.post("/resources/{resource_id}/deactivate")
    def deactivate_resource(
        resource_id: str,
        facade: ApplicationFacade = Depends(facade_dependency),
    ) -> dict[str, Any]:
        return _payload(
            facade.update_resource(ResourceUpdateCommand(resource_id=resource_id, active=False))
        )

    @router.post("/availability-rules", status_code=status.HTTP_201_CREATED)
    def create_availability_rule(
        body: AvailabilityRuleCreateRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        facade: ApplicationFacade = Depends(facade_dependency),
        idempotency: IdempotentCommandExecutor = Depends(idempotency_dependency),
    ) -> dict[str, Any]:
        return idempotency.execute(
            scope="availability_rule.create",
            key=idempotency_key,
            request_payload=_json_body(body),
            action=lambda: _payload(
                facade.create_availability_rule(
                    AvailabilityRuleCreateCommand(**body.model_dump())
                )
            ),
        )

    @router.patch("/availability-rules/{rule_id}")
    def update_availability_rule(
        rule_id: str,
        body: AvailabilityRuleUpdateRequest,
        facade: ApplicationFacade = Depends(facade_dependency),
    ) -> dict[str, Any]:
        return _payload(
            facade.update_availability_rule(
                AvailabilityRuleUpdateCommand(
                    rule_id=rule_id,
                    **body.model_dump(exclude_unset=True),
                )
            )
        )

    @router.post("/availability-rules/{rule_id}/deactivate")
    def deactivate_availability_rule(
        rule_id: str,
        facade: ApplicationFacade = Depends(facade_dependency),
    ) -> dict[str, Any]:
        return _payload(facade.deactivate_availability_rule(rule_id))

    @router.post("/work-packages", status_code=status.HTTP_201_CREATED)
    def create_work_package(
        body: WorkPackageCreateRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        facade: ApplicationFacade = Depends(facade_dependency),
        idempotency: IdempotentCommandExecutor = Depends(idempotency_dependency),
    ) -> dict[str, Any]:
        return idempotency.execute(
            scope="work_package.create",
            key=idempotency_key,
            request_payload=_json_body(body),
            action=lambda: _payload(
                facade.create_work_package(
                    WorkPackageCreateCommand(**body.model_dump(exclude_unset=True))
                )
            ),
        )

    @router.patch("/work-packages/{reference}")
    def update_work_package(
        reference: str,
        body: WorkPackageUpdateRequest,
        facade: ApplicationFacade = Depends(facade_dependency),
    ) -> dict[str, Any]:
        return _payload(
            facade.update_work_package(
                WorkPackageUpdateCommand(
                    reference=reference,
                    **body.model_dump(exclude_unset=True),
                )
            )
        )

    @router.post("/work-packages/{reference}/weekly-loads/proposal")
    def propose_work_package_weekly_loads(
        reference: str,
        facade: ApplicationFacade = Depends(facade_dependency),
    ) -> dict[str, Any]:
        return _payload(facade.propose_work_package_weekly_loads(reference))

    @router.put("/work-packages/{reference}/weekly-loads")
    def replace_work_package_weekly_loads(
        reference: str,
        body: WorkPackageWeeklyLoadReplaceRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        facade: ApplicationFacade = Depends(facade_dependency),
        idempotency: IdempotentCommandExecutor = Depends(stable_idempotency),
    ) -> dict[str, Any]:
        command = WorkPackageWeeklyLoadReplaceCommand(
            reference=reference,
            expected_version=body.expected_version,
            origin=body.origin,
            loads=tuple(
                WeeklyLoadValue(week_start=row.week_start, hours=row.hours)
                for row in body.loads
            ),
        )
        return idempotency.execute(
            scope="work_package.weekly_load.replace",
            key=idempotency_key,
            request_payload=_json_body(body),
            action=lambda: _payload(
                facade.replace_work_package_weekly_loads(command)
            ),
        )

    @router.post("/demands", status_code=status.HTTP_201_CREATED)
    def create_demand(
        body: DemandCreateRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        facade: ApplicationFacade = Depends(facade_dependency),
        idempotency: IdempotentCommandExecutor = Depends(idempotency_dependency),
        competencies: CompetencyCatalogService = Depends(competency_dependency),
    ) -> dict[str, Any]:
        selection_supplied = "required_competency_ids" in body.model_fields_set
        competency_ids = tuple(body.required_competency_ids or ())
        line_inputs = _demand_line_inputs(body.lines, competencies)
        values = body.model_dump(exclude={"required_competency_ids", "lines"})
        if selection_supplied:
            values["required_competencies"] = competencies.snapshot_text(competency_ids)
        if line_inputs is not None:
            values["lines"] = line_inputs

        def action() -> dict[str, Any]:
            result = facade.create_demand(DemandCreateCommand(**values))
            if selection_supplied and line_inputs is None:
                competencies.assign_demand(result.demand_number, competency_ids)
            return _payload(result)

        return idempotency.execute(
            scope="demand.create",
            key=idempotency_key,
            request_payload=_json_body(body),
            action=action,
        )

    @router.patch("/demands/{number}")
    def update_demand(
        number: str,
        body: DemandUpdateRequest,
        facade: ApplicationFacade = Depends(facade_dependency),
        competencies: CompetencyCatalogService = Depends(competency_dependency),
    ) -> dict[str, Any]:
        selection_supplied = "required_competency_ids" in body.model_fields_set
        competency_ids = tuple(body.required_competency_ids or ())
        line_inputs = (
            _demand_line_inputs(body.lines, competencies)
            if "lines" in body.model_fields_set
            else None
        )
        values = body.model_dump(
            exclude_unset=True,
            exclude={"required_competency_ids", "lines"},
        )
        if selection_supplied:
            values["required_competencies"] = competencies.snapshot_text(competency_ids)
        if "lines" in body.model_fields_set:
            values["lines"] = line_inputs
        comment = str(values.pop("comment", "Demande modifiée via API") or "")
        command = DemandUpdateCommand(number=number, comment=comment, **values)
        result = facade.update_demand(command)
        if selection_supplied and "lines" not in body.model_fields_set:
            competencies.assign_demand(number, competency_ids)
        return _payload(result)

    @router.put("/demands/{number}/periods")
    def replace_demand_periods(
        number: str,
        body: DemandPeriodsReplaceRequest,
        facade: ApplicationFacade = Depends(facade_dependency),
    ) -> dict[str, Any]:
        command = DemandPeriodsReplaceCommand(
            number=number,
            periods=tuple(DemandPeriodInput(**period.model_dump()) for period in body.periods),
            expected_request_version=body.expected_request_version,
        )
        return _payload(facade.replace_demand_periods(command))

    @router.put("/demands/{number}/alternative-groups/{alternative_group}/selection")
    def select_demand_alternative(
        number: str,
        alternative_group: str,
        body: DemandAlternativeSelectionRequest,
        facade: ApplicationFacade = Depends(facade_dependency),
    ) -> dict[str, Any]:
        return _payload(
            facade.select_demand_alternative(
                DemandAlternativeSelectCommand(
                    number=number,
                    alternative_group=alternative_group,
                    period_id=body.period_id,
                    expected_operational_version=body.expected_operational_version,
                )
            )
        )

    @router.put("/demands/{number}/lines/{line_id}/periods")
    def replace_demand_line_periods(
        number: str,
        line_id: str,
        body: DemandPeriodsReplaceRequest,
        facade: ApplicationFacade = Depends(facade_dependency),
    ) -> dict[str, Any]:
        command = DemandPeriodsReplaceCommand(
            number=number,
            request_line_id=line_id,
            periods=tuple(
                DemandPeriodInput(**period.model_dump())
                for period in body.periods
            ),
            expected_request_version=body.expected_request_version,
        )
        return _payload(facade.replace_demand_periods(command))

    @router.put(
        "/demands/{number}/lines/{line_id}/alternative-groups/"
        "{alternative_group}/selection"
    )
    def select_demand_line_alternative(
        number: str,
        line_id: str,
        alternative_group: str,
        body: DemandAlternativeSelectionRequest,
        facade: ApplicationFacade = Depends(facade_dependency),
    ) -> dict[str, Any]:
        return _payload(
            facade.select_demand_alternative(
                DemandAlternativeSelectCommand(
                    number=number,
                    request_line_id=line_id,
                    alternative_group=alternative_group,
                    period_id=body.period_id,
                    expected_operational_version=body.expected_operational_version,
                )
            )
        )

    @router.put(
        "/demands/{number}/operational-alternative-groups/"
        "{alternative_group}/selection"
    )
    def select_demand_operational_alternative(
        number: str,
        alternative_group: str,
        body: DemandAlternativeSelectionRequest,
        facade: ApplicationFacade = Depends(facade_dependency),
    ) -> dict[str, Any]:
        return _payload(
            facade.select_demand_alternative(
                DemandAlternativeSelectCommand(
                    number=number,
                    alternative_group=alternative_group,
                    period_id=body.period_id,
                    expected_operational_version=body.expected_operational_version,
                    operational=True,
                )
            )
        )

    @router.put(
        "/demands/{number}/lines/{line_id}/operational-alternative-groups/"
        "{alternative_group}/selection"
    )
    def select_demand_line_operational_alternative(
        number: str,
        line_id: str,
        alternative_group: str,
        body: DemandAlternativeSelectionRequest,
        facade: ApplicationFacade = Depends(facade_dependency),
    ) -> dict[str, Any]:
        return _payload(
            facade.select_demand_alternative(
                DemandAlternativeSelectCommand(
                    number=number,
                    request_line_id=line_id,
                    alternative_group=alternative_group,
                    period_id=body.period_id,
                    expected_operational_version=body.expected_operational_version,
                    operational=True,
                )
            )
        )

    @router.put("/demands/{number}/operational-confirmation")
    def set_demand_operational_confirmation(
        number: str,
        body: DemandOperationalConfirmationRequest,
        facade: ApplicationFacade = Depends(facade_dependency),
    ) -> dict[str, Any]:
        return _payload(
            facade.set_demand_operational_confirmation(
                DemandOperationalConfirmationCommand(
                    number=number,
                    confirmation=body.confirmation,
                    period_id=body.period_id,
                    expected_operational_version=body.expected_operational_version,
                )
            )
        )

    @router.put("/demands/{number}/lines/{line_id}/operational-confirmation")
    def set_demand_line_operational_confirmation(
        number: str,
        line_id: str,
        body: DemandOperationalConfirmationRequest,
        facade: ApplicationFacade = Depends(facade_dependency),
    ) -> dict[str, Any]:
        return _payload(
            facade.set_demand_operational_confirmation(
                DemandOperationalConfirmationCommand(
                    number=number,
                    request_line_id=line_id,
                    period_id=body.period_id,
                    confirmation=body.confirmation,
                    expected_operational_version=body.expected_operational_version,
                )
            )
        )

    @router.get("/demands/{number}/workflow-actions")
    def demand_workflow_actions(
        number: str,
        facade: ApplicationFacade = Depends(facade_dependency),
    ) -> dict[str, Any]:
        return _payload(facade.demand_workflow_state(number))

    @router.post("/demands/{number}/submit")
    def submit_demand(
        number: str,
        body: DemandWorkflowVersionRequest | None = None,
        facade: ApplicationFacade = Depends(facade_dependency),
    ) -> dict[str, Any]:
        return _payload(
            facade.submit_demand(
                DemandSubmitCommand(
                    number=number,
                    expected_version=body.expected_version if body is not None else None,
                )
            )
        )

    @router.post("/demands/{number}/approve")
    def approve_demand(
        number: str,
        body: DemandApprovalRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        facade: ApplicationFacade = Depends(facade_dependency),
        idempotency: IdempotentCommandExecutor = Depends(stable_idempotency),
    ) -> Any:
        request_payload = {
            "operation": "APPROVE_ELIGIBLE_REQUIREMENTS",
            "demand_number": number,
            "body": _json_body(body),
        }
        result = idempotency.execute(
            scope="demand_approval.approve_eligible",
            key=idempotency_key,
            request_payload=request_payload,
            action=lambda: _payload(
                facade.approve_demand_quorum(
                    DemandApproveCommand(
                        number=number,
                        comment=body.comment,
                        expected_version=body.expected_version,
                        expected_planning_version=getattr(body, "expected_planning_version", None),
                    )
                )
            ),
        )
        if result.get("conflict_code"):
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "error": {
                        "code": result["conflict_code"],
                        "message": result.get("conflict_message") or "Conflit d'approbation.",
                        "context": {
                            "approval_cycle_id": result.get("approval_cycle_id"),
                            "request_version": result.get("request_version"),
                        },
                    }
                },
            )
        return result

    @router.post("/demands/{number}/approval-votes")
    def vote_demand_approval(
        number: str,
        body: DemandApprovalVoteRequest,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        facade: ApplicationFacade = Depends(facade_dependency),
        idempotency: IdempotentCommandExecutor = Depends(stable_idempotency),
    ) -> Any:
        request_payload = {
            "operation": "VOTE_APPROVAL_REQUIREMENTS",
            "demand_number": number,
            "body": _json_body(body),
        }
        result = idempotency.execute(
            scope="demand_approval.vote",
            key=idempotency_key,
            request_payload=request_payload,
            action=lambda: _payload(
                facade.vote_demand_approval(
                    ApprovalVoteCommand(
                        workforce_request_id=number,
                        approval_cycle_id=body.approval_cycle_id,
                        expected_request_version=body.expected_request_version,
                        requirement_ids=tuple(body.requirement_ids),
                        request_line_ids=tuple(body.request_line_ids),
                        decision=body.decision,
                        comment=body.comment,
                        expected_planning_version=body.expected_planning_version,
                    )
                )
            ),
        )
        if result.get("conflict_code"):
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "error": {
                        "code": result["conflict_code"],
                        "message": result.get("conflict_message") or "Conflit d'approbation.",
                        "context": {
                            "approval_cycle_id": result.get("approval_cycle_id"),
                            "request_version": result.get("request_version"),
                        },
                    }
                },
            )
        return result

    @router.post("/demands/{number}/emergency-plan")
    def emergency_plan_demand(
        number: str,
        body: DemandWorkflowRequiredCommentRequest,
        facade: ApplicationFacade = Depends(facade_dependency),
    ) -> dict[str, Any]:
        action = getattr(facade, "emergency_plan_demand", None)
        if action is None:
            raise RuntimeError("Emergency demand workflow is not configured")
        return _payload(
            action(
                DemandEmergencyOverrideCommand(
                    number=number,
                    comment=body.comment,
                    expected_version=body.expected_version,
                )
            )
        )

    @router.post("/demands/{number}/correction")
    def request_demand_correction(
        number: str,
        body: DemandWorkflowRequiredCommentRequest,
        facade: ApplicationFacade = Depends(facade_dependency),
    ) -> dict[str, Any]:
        return _payload(
            facade.request_demand_correction(
                DemandCorrectionCommand(
                    number=number,
                    comment=body.comment,
                    expected_version=body.expected_version,
                )
            )
        )

    @router.post("/demands/{number}/request-cancellation")
    def request_demand_cancellation(
        number: str,
        body: DemandCancellationRequest,
        facade: ApplicationFacade = Depends(facade_dependency),
    ) -> dict[str, Any]:
        return _payload(
            facade.request_demand_cancellation(
                DemandCancellationRequestCommand(
                    number=number,
                    reason=body.reason,
                    expected_version=body.expected_version,
                )
            )
        )

    @router.post("/demands/{number}/accept-cancellation")
    def accept_demand_cancellation(
        number: str,
        body: DemandCancellationAcceptRequest,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        facade: ApplicationFacade = Depends(facade_dependency),
        idempotency: IdempotentCommandExecutor = Depends(stable_idempotency),
    ) -> dict[str, Any]:
        request_payload = {
            "operation": "ACCEPT_CANCELLATION",
            "demand_number": number,
            "body": _json_body(body),
        }
        return idempotency.execute(
            scope="demand_cancellation.accept",
            key=idempotency_key,
            request_payload=request_payload,
            action=lambda: _payload(
                facade.accept_demand_cancellation(
                    DemandCancellationAcceptCommand(
                        number=number,
                        cancellation_request_id=body.cancellation_request_id,
                        comment=body.comment,
                        expected_version=body.expected_version,
                        expected_planning_version=body.expected_planning_version,
                        correlation_id=idempotency_key,
                    )
                )
            ),
        )

    @router.post("/demands/{number}/reject-cancellation")
    def reject_demand_cancellation(
        number: str,
        body: DemandCancellationRejectRequest,
        facade: ApplicationFacade = Depends(facade_dependency),
    ) -> dict[str, Any]:
        return _payload(
            facade.reject_demand_cancellation(
                DemandCancellationRejectCommand(
                    number=number,
                    cancellation_request_id=body.cancellation_request_id,
                    comment=body.comment,
                    expected_version=body.expected_version,
                )
            )
        )

    @router.post("/demands/{number}/cancel")
    def cancel_demand(
        number: str,
        body: DemandWorkflowVersionRequest | None = None,
        facade: ApplicationFacade = Depends(facade_dependency),
    ) -> dict[str, Any]:
        return _payload(
            facade.cancel_demand(
                DemandCancelCommand(
                    number=number,
                    expected_version=body.expected_version if body is not None else None,
                )
            )
        )

    @router.post("/segments", status_code=status.HTTP_201_CREATED)
    def create_segment(
        body: SegmentCreateRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        facade: ApplicationFacade = Depends(facade_dependency),
        idempotency: IdempotentCommandExecutor = Depends(idempotency_dependency),
        competencies: CompetencyCatalogService = Depends(competency_dependency),
    ) -> dict[str, Any]:
        values, selection_supplied, competency_id = _segment_command_values(
            body,
            competencies,
        )

        def action() -> dict[str, Any]:
            result = facade.create_segment(SegmentCreateCommand(**values))
            if selection_supplied:
                competencies.assign_segment(result.segment_id, competency_id)
            return _payload(result)

        return idempotency.execute(
            scope="segment.create",
            key=idempotency_key,
            request_payload=_json_body(body),
            action=action,
        )

    @router.patch("/segments/{segment_id}")
    def update_segment(
        segment_id: str,
        body: SegmentUpdateRequest,
        facade: ApplicationFacade = Depends(facade_dependency),
        competencies: CompetencyCatalogService = Depends(competency_dependency),
    ) -> dict[str, Any]:
        values, selection_supplied, competency_id = _segment_command_values(
            body,
            competencies,
        )
        result = facade.update_segment(
            SegmentUpdateCommand(segment_id=segment_id, **values)
        )
        if selection_supplied:
            competencies.assign_segment(segment_id, competency_id)
        return _payload(result)

    @router.post("/segments/{segment_id}/cancel")
    def cancel_segment(
        segment_id: str,
        facade: ApplicationFacade = Depends(facade_dependency),
    ) -> dict[str, Any]:
        return _payload(facade.cancel_segment(SegmentCancelCommand(segment_id)))

    @router.post("/segments/{segment_id}/assign")
    def assign_segment(
        segment_id: str,
        body: SegmentAssignRequest,
        facade: ApplicationFacade = Depends(facade_dependency),
    ) -> dict[str, Any]:
        return _payload(
            facade.assign_segment(
                SegmentAssignCommand(
                    segment_id=segment_id,
                    technician=getattr(body, "technician", None) or "",
                    resource_id=body.resource_id,
                )
            )
        )

    @router.post("/segments/{segment_id}/allocations", status_code=status.HTTP_201_CREATED)
    def create_allocation(
        segment_id: str,
        body: ManualAllocationRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        facade: ApplicationFacade = Depends(facade_dependency),
        idempotency: IdempotentCommandExecutor = Depends(idempotency_dependency),
    ) -> dict[str, Any]:
        request_payload = {
            "segment_id": segment_id,
            "body": _json_body(body),
        }
        return idempotency.execute(
            scope="manual_allocation.create",
            key=idempotency_key,
            request_payload=request_payload,
            action=lambda: _payload(
                facade.create_allocation(
                    ManualAllocationCreateCommand(
                        segment_id=segment_id,
                        **{
                            **body.model_dump(),
                            "technician": body.technician or "",
                        },
                    )
                )
            ),
        )

    @router.put("/allocations/{allocation_id}")
    def update_allocation(
        allocation_id: str,
        body: ManualAllocationRequest,
        facade: ApplicationFacade = Depends(facade_dependency),
    ) -> dict[str, Any]:
        values = body.model_dump()
        values["technician"] = body.technician or ""
        confirmation = values.pop("confirmation")
        return _payload(
            facade.update_allocation(
                ManualAllocationUpdateCommand(
                    allocation_id=allocation_id,
                    confirmation=confirmation,
                    clear_confirmation_override=confirmation is None,
                    **values,
                )
            )
        )

    @router.post("/allocations/{allocation_id}/evaluate-drop")
    def evaluate_allocation_drop(
        allocation_id: str,
        body: AllocationDropEvaluateRequest,
        facade: ApplicationFacade = Depends(facade_dependency),
    ) -> dict[str, Any]:
        return _payload(
            facade.evaluate_allocation_drop(
                AllocationDropEvaluateCommand(
                    allocation_id=allocation_id,
                    **body.model_dump(),
                )
            )
        )

    @router.post(
        "/allocations/{allocation_id}/propose-window-extension",
        status_code=status.HTTP_200_OK,
    )
    def propose_allocation_window_extension(
        allocation_id: str,
        body: AllocationWindowExtensionProposalRequest,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        facade: ApplicationFacade = Depends(facade_dependency),
        idempotency: IdempotentCommandExecutor = Depends(stable_idempotency),
    ) -> dict[str, Any]:
        request_payload = {
            "operation": "PROPOSE_WINDOW_EXTENSION",
            "allocation_id": allocation_id,
            "body": _json_body(body),
        }
        return idempotency.execute(
            scope="manual_allocation.propose_window_extension",
            key=idempotency_key,
            request_payload=request_payload,
            action=lambda: _payload(
                facade.propose_allocation_window_extension(
                    AllocationWindowExtensionProposalCommand(
                        allocation_id=allocation_id,
                        correlation_id=idempotency_key,
                        **body.model_dump(),
                    )
                )
            ),
        )

    @router.post(
        "/allocations/{allocation_id}/extend-and-move",
        status_code=status.HTTP_200_OK,
    )
    def extend_and_move_allocation(
        allocation_id: str,
        body: AllocationExtendMoveRequest,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        facade: ApplicationFacade = Depends(facade_dependency),
        idempotency: IdempotentCommandExecutor = Depends(stable_idempotency),
    ) -> dict[str, Any]:
        request_payload = {
            "operation": "EXTEND_AND_MOVE",
            "allocation_id": allocation_id,
            "body": _json_body(body),
        }
        return idempotency.execute(
            scope="manual_allocation.extend_and_move",
            key=idempotency_key,
            request_payload=request_payload,
            action=lambda: _payload(
                facade.extend_and_move_allocation(
                    AllocationExtendMoveCommand(
                        allocation_id=allocation_id,
                        correlation_id=idempotency_key,
                        **body.model_dump(),
                    )
                )
            ),
        )

    @router.post(
        "/allocations/{allocation_id}/split",
        status_code=status.HTTP_201_CREATED,
    )
    def split_allocation(
        allocation_id: str,
        body: AllocationSplitRequest,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        facade: ApplicationFacade = Depends(facade_dependency),
        idempotency: IdempotentCommandExecutor = Depends(stable_idempotency),
    ) -> dict[str, Any]:
        request_payload = {
            "operation": "SPLIT",
            "allocation_id": allocation_id,
            "body": _json_body(body),
        }
        return idempotency.execute(
            scope="manual_allocation.split",
            key=idempotency_key,
            request_payload=request_payload,
            action=lambda: _payload(
                facade.split_allocation(
                    AllocationSplitCommand(
                        allocation_id=allocation_id,
                        correlation_id=idempotency_key,
                        **body.model_dump(),
                    )
                )
            ),
        )

    @router.post(
        "/allocations/{allocation_id}/duplicate",
        status_code=status.HTTP_201_CREATED,
    )
    def duplicate_allocation(
        allocation_id: str,
        body: AllocationDuplicateRequest,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        facade: ApplicationFacade = Depends(facade_dependency),
        idempotency: IdempotentCommandExecutor = Depends(stable_idempotency),
    ) -> dict[str, Any]:
        request_payload = {
            "operation": "DUPLICATE",
            "allocation_id": allocation_id,
            "body": _json_body(body),
        }
        return idempotency.execute(
            scope="manual_allocation.duplicate",
            key=idempotency_key,
            request_payload=request_payload,
            action=lambda: _payload(
                facade.duplicate_allocation(
                    AllocationDuplicateCommand(
                        allocation_id=allocation_id,
                        correlation_id=idempotency_key,
                        **body.model_dump(),
                    )
                )
            ),
        )

    @router.post("/allocations/{allocation_id}/move")
    def move_allocation(
        allocation_id: str,
        body: AllocationMoveRequest,
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
            min_length=1,
        ),
        facade: ApplicationFacade = Depends(facade_dependency),
        idempotency: IdempotentCommandExecutor = Depends(stable_idempotency),
    ) -> dict[str, Any]:
        def action() -> dict[str, Any]:
            return _payload(
                facade.move_allocation(
                    ManualAllocationMoveCommand(
                        allocation_id=allocation_id,
                        technician=body.technician or "",
                        day=body.day,
                        resource_id=body.resource_id,
                        outside_standard_hours=body.outside_standard_hours,
                        expected_planning_version=body.expected_planning_version,
                    )
                )
            )

        if not idempotency_key:
            return action()
        return idempotency.execute(
            scope="manual_allocation.move",
            key=idempotency_key,
            request_payload={
                "operation": "MOVE",
                "allocation_id": allocation_id,
                "body": _json_body(body),
            },
            action=action,
        )

    @router.post("/allocations/{allocation_id}/release")
    def release_allocation(
        allocation_id: str,
        expected_planning_version: int | None = Query(default=None, ge=1),
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
            min_length=1,
        ),
        facade: ApplicationFacade = Depends(facade_dependency),
        idempotency: IdempotentCommandExecutor = Depends(stable_idempotency),
    ) -> dict[str, Any]:
        def action() -> dict[str, Any]:
            return _payload(
                facade.release_allocation(
                    ManualAllocationReleaseCommand(
                        allocation_id,
                        expected_planning_version=expected_planning_version,
                    )
                )
            )

        if not idempotency_key:
            return action()
        return idempotency.execute(
            scope="manual_allocation.release",
            key=idempotency_key,
            request_payload={
                "operation": "RELEASE",
                "allocation_id": allocation_id,
                "expected_planning_version": expected_planning_version,
            },
            action=action,
        )

    @router.delete("/allocations/{allocation_id}")
    def delete_allocation(
        allocation_id: str,
        expected_planning_version: int | None = Query(default=None, ge=1),
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
            min_length=1,
        ),
        facade: ApplicationFacade = Depends(facade_dependency),
        idempotency: IdempotentCommandExecutor = Depends(stable_idempotency),
    ) -> dict[str, Any]:
        def action() -> dict[str, Any]:
            return _payload(
                facade.delete_allocation(
                    ManualAllocationDeleteCommand(
                        allocation_id,
                        expected_planning_version=expected_planning_version,
                    )
                )
            )

        if not idempotency_key:
            return action()
        return idempotency.execute(
            scope="manual_allocation.delete",
            key=idempotency_key,
            request_payload={
                "operation": "DELETE",
                "allocation_id": allocation_id,
                "expected_planning_version": expected_planning_version,
            },
            action=action,
        )

    @router.post("/quick-shifts", status_code=status.HTTP_201_CREATED)
    def create_quick_shift(
        body: QuickShiftRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        facade: ApplicationFacade = Depends(facade_dependency),
        idempotency: IdempotentCommandExecutor = Depends(idempotency_dependency),
    ) -> dict[str, Any]:
        return idempotency.execute(
            scope="quick_shift.create",
            key=idempotency_key,
            request_payload=_json_body(body),
            action=lambda: _payload(
                facade.create_quick_shift(
                    QuickShiftCreateCommand(**body.model_dump())
                )
            ),
        )

    @router.post("/planning/rebuild")
    def rebuild_planning(
        facade: ApplicationFacade = Depends(facade_dependency),
    ) -> dict[str, Any]:
        return _payload(facade.rebuild_planning(PlanningRebuildCommand()))

    return router