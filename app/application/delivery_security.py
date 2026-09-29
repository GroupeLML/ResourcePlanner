from __future__ import annotations

from app.domain.delivery import DeliveryItem, DeliveryPlan

from .delivery_contracts import DeliveryAction, DeliveryActorContext, delivery_actions_for
from .security import (
    AuthPrincipal,
    PERMISSION_CONTRIBUTE_DELIVERY,
    PERMISSION_MANAGE_DELIVERY,
)


_PLAN_MANAGEMENT_ACTIONS = frozenset(
    {
        DeliveryAction.MANAGE_PLAN_LIFECYCLE,
        DeliveryAction.ASSIGN_TEAM_LEAD,
        DeliveryAction.SET_PLAN_PRIORITY_DUE_DATE,
    }
)
_CONTEXTUAL_ACTIONS = frozenset(set(DeliveryAction) - set(_PLAN_MANAGEMENT_ACTIONS))


def authorized_delivery_actions_for(
    principal: AuthPrincipal,
    plan: DeliveryPlan,
    item: DeliveryItem | None = None,
) -> frozenset[DeliveryAction]:
    """Bind the 362A contextual policy to global RBAC permissions."""

    if not principal.local_user_id:
        return frozenset()
    conceptual = delivery_actions_for(
        DeliveryActorContext(user_id=principal.local_user_id, roles=principal.roles),
        plan,
        item,
    )
    allowed: set[DeliveryAction] = set()
    if principal.has_permission(PERMISSION_MANAGE_DELIVERY):
        allowed.update(conceptual.intersection(_PLAN_MANAGEMENT_ACTIONS))
    if principal.has_permission(PERMISSION_CONTRIBUTE_DELIVERY):
        allowed.update(conceptual.intersection(_CONTEXTUAL_ACTIONS))
    return frozenset(allowed)
