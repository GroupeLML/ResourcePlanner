from __future__ import annotations

from app.domain.verification import VerificationScope

from .security import (
    AuthPrincipal,
    PERMISSION_CONTRIBUTE_DELIVERY,
    PERMISSION_EXECUTE_VERIFICATION,
    PERMISSION_MANAGE_VERIFICATION,
    PERMISSION_READ,
    ROLE_ADMIN,
)
from .verification_contracts import (
    VerificationAction,
    VerificationActorContext,
    VerificationAuthorityContext,
    verification_actions_for,
)


_MANAGEMENT_ACTIONS = frozenset(
    {
        VerificationAction.PILOT_SCOPE,
        VerificationAction.DEFINE_STORY_REQUIREMENTS,
        VerificationAction.REVISE_REQUIREMENTS,
        VerificationAction.ASSIGN_EXECUTORS,
        VerificationAction.WITHDRAW_REQUIREMENTS,
        VerificationAction.REQUEST_RETEST,
    }
)
_EXECUTION_ACTIONS = frozenset(
    {
        VerificationAction.RECORD_RESULT,
        VerificationAction.ADD_EVIDENCE,
    }
)


def authorized_verification_actions_for(
    principal: AuthPrincipal,
    scope: VerificationScope,
    *,
    project_scope_authorized: bool = False,
    assigned_executor: bool = False,
    story_closure_authorized: bool = False,
) -> frozenset[VerificationAction]:
    """Bind ADR-023 contextual actions to explicit Verification RBAC permissions."""

    actor_user_id = str(principal.local_user_id or "").strip()
    if not actor_user_id:
        return frozenset()
    conceptual = verification_actions_for(
        VerificationActorContext(actor_user_id, tuple(principal.roles)),
        scope,
        VerificationAuthorityContext(
            story_closure_authorized=story_closure_authorized,
            project_scope_authorized=project_scope_authorized,
            assigned_executor=assigned_executor,
            execution_authorized=principal.has_permission(
                PERMISSION_EXECUTE_VERIFICATION
            ),
        ),
    )
    if ROLE_ADMIN in principal.roles:
        return conceptual

    allowed: set[VerificationAction] = set()
    if (
        principal.has_permission(PERMISSION_READ)
        and VerificationAction.VIEW_SCOPE in conceptual
    ):
        allowed.add(VerificationAction.VIEW_SCOPE)
    if principal.has_permission(PERMISSION_MANAGE_VERIFICATION):
        allowed.update(conceptual.intersection(_MANAGEMENT_ACTIONS))
    if principal.has_permission(PERMISSION_EXECUTE_VERIFICATION):
        allowed.update(conceptual.intersection(_EXECUTION_ACTIONS))
    if (
        story_closure_authorized
        and principal.has_permission(PERMISSION_CONTRIBUTE_DELIVERY)
        and VerificationAction.RECORD_STORY_DECISION in conceptual
    ):
        allowed.add(VerificationAction.RECORD_STORY_DECISION)
        if VerificationAction.DEFINE_STORY_REQUIREMENTS in conceptual:
            allowed.add(VerificationAction.DEFINE_STORY_REQUIREMENTS)
    return frozenset(allowed)
