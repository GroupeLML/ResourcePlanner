from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


APPROVAL_SCOPE_CODE_ELECTRICAL_INSTALLATION = "ELECTRICAL_INSTALLATION"
APPROVAL_SCOPE_CODE_AUTOMATION = "AUTOMATION"
APPROVER_SOURCE_SCOPE = "APPROVAL_SCOPE"
APPROVER_SOURCE_RESOURCE = "PROPOSED_RESOURCE"
PERMISSION_APPROVE_DEMANDS = "approve_demands"

DIAGNOSTIC_LINE_INACTIVE = "line_inactive"
DIAGNOSTIC_TASK_REFERENCE_MISSING = "task_reference_missing"
DIAGNOSTIC_TASK_NOT_FOUND = "task_not_found"
DIAGNOSTIC_TASK_INACTIVE = "task_inactive"
DIAGNOSTIC_RESOURCE_CLASS_MISSING = "resource_class_missing"
DIAGNOSTIC_RESOURCE_CLASS_NOT_FOUND = "resource_class_not_found"
DIAGNOSTIC_RESOURCE_CLASS_INACTIVE = "resource_class_inactive"
DIAGNOSTIC_SCOPE_UNMAPPED = "approval_scope_unmapped"
DIAGNOSTIC_SCOPE_AMBIGUOUS = "approval_scope_ambiguous"
DIAGNOSTIC_SCOPE_INACTIVE = "approval_scope_inactive"
DIAGNOSTIC_APPROVER_NOT_FOUND = "approver_not_found"
DIAGNOSTIC_APPROVER_INACTIVE = "approver_inactive"
DIAGNOSTIC_APPROVER_PERMISSION_MISSING = "approver_permission_missing"
DIAGNOSTIC_NO_ELIGIBLE_APPROVER = "no_eligible_approver"


@dataclass(frozen=True, slots=True)
class ApprovalScopeCandidate:
    scope_id: str
    active: bool


@dataclass(frozen=True, slots=True)
class ApprovalRoutingUser:
    user_id: str
    active: bool
    permissions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EligibleApprover:
    user_id: str
    sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ApprovalLineResolution:
    approval_scope_id: str | None
    eligible_approvers: tuple[EligibleApprover, ...]
    diagnostics: tuple[str, ...]
    blocked: bool


def suggested_approval_scope_code(task_code: object) -> str | None:
    """Return only a classification suggestion, never an authorization decision."""

    raw = str(task_code or "").strip()
    if not raw or not raw.isdigit():
        return None
    value = int(raw)
    if 110 <= value <= 119:
        return APPROVAL_SCOPE_CODE_ELECTRICAL_INSTALLATION
    if 210 <= value <= 219:
        return APPROVAL_SCOPE_CODE_AUTOMATION
    return None


def resolve_line_approvers(
    *,
    line_active: bool,
    task_catalog_item_id: str | None,
    task_exists: bool,
    task_active: bool,
    scope_candidates: Sequence[ApprovalScopeCandidate],
    scope_approver_user_ids: Sequence[str],
    users: Mapping[str, ApprovalRoutingUser],
    resource_approver_user_ids: Sequence[str] = (),
) -> ApprovalLineResolution:
    """Resolve admissible approvers without persistence or workflow side effects.

    resource_approver_user_ids exists only to keep the union contract stable for the
    future Resource-to-AppUser approval-authority extension.
    """

    diagnostics: list[str] = []
    if not line_active:
        diagnostics.append(DIAGNOSTIC_LINE_INACTIVE)
        return ApprovalLineResolution(None, (), tuple(diagnostics), True)

    task_id = str(task_catalog_item_id or "").strip()
    if not task_id:
        diagnostics.append(DIAGNOSTIC_TASK_REFERENCE_MISSING)
        return ApprovalLineResolution(None, (), tuple(diagnostics), True)
    if not task_exists:
        diagnostics.append(DIAGNOSTIC_TASK_NOT_FOUND)
        return ApprovalLineResolution(None, (), tuple(diagnostics), True)
    if not task_active:
        diagnostics.append(DIAGNOSTIC_TASK_INACTIVE)
        return ApprovalLineResolution(None, (), tuple(diagnostics), True)

    candidates = tuple(sorted(scope_candidates, key=lambda row: row.scope_id))
    if not candidates:
        diagnostics.append(DIAGNOSTIC_SCOPE_UNMAPPED)
        return ApprovalLineResolution(None, (), tuple(diagnostics), True)
    if len(candidates) != 1:
        diagnostics.append(DIAGNOSTIC_SCOPE_AMBIGUOUS)
        return ApprovalLineResolution(None, (), tuple(diagnostics), True)

    scope = candidates[0]
    if not scope.active:
        diagnostics.append(DIAGNOSTIC_SCOPE_INACTIVE)
        return ApprovalLineResolution(scope.scope_id, (), tuple(diagnostics), True)

    sources_by_user: dict[str, set[str]] = {}
    for user_id in scope_approver_user_ids:
        normalized = str(user_id or "").strip()
        if normalized:
            sources_by_user.setdefault(normalized, set()).add(APPROVER_SOURCE_SCOPE)
    for user_id in resource_approver_user_ids:
        normalized = str(user_id or "").strip()
        if normalized:
            sources_by_user.setdefault(normalized, set()).add(APPROVER_SOURCE_RESOURCE)

    eligible: list[EligibleApprover] = []
    for user_id in sorted(sources_by_user):
        user = users.get(user_id)
        if user is None:
            diagnostics.append(f"{DIAGNOSTIC_APPROVER_NOT_FOUND}:{user_id}")
            continue
        if not user.active:
            diagnostics.append(f"{DIAGNOSTIC_APPROVER_INACTIVE}:{user_id}")
            continue
        if PERMISSION_APPROVE_DEMANDS not in user.permissions:
            diagnostics.append(f"{DIAGNOSTIC_APPROVER_PERMISSION_MISSING}:{user_id}")
            continue
        eligible.append(
            EligibleApprover(
                user_id=user_id,
                sources=tuple(sorted(sources_by_user[user_id])),
            )
        )

    if not eligible:
        diagnostics.append(DIAGNOSTIC_NO_ELIGIBLE_APPROVER)
    return ApprovalLineResolution(
        approval_scope_id=scope.scope_id,
        eligible_approvers=tuple(eligible),
        diagnostics=tuple(diagnostics),
        blocked=not bool(eligible),
    )
