from __future__ import annotations

from hashlib import sha256

from .project_managers import EffectiveProjectManager, EffectiveProjectManagers


def _stable_identity(manager: EffectiveProjectManager) -> str | None:
    """Use the same source-independent identity for ERP principal and RP co-manager."""
    if manager.employee_external_id:
        return f"employee:{manager.employee_external_id}"
    if manager.business_contact_id:
        return f"contact:{manager.business_contact_id}"
    if manager.app_user_id:
        return f"user:{manager.app_user_id}"
    return None


def project_manager_color_marker(
    managers: EffectiveProjectManagers | None,
) -> tuple[str | None, str | None]:
    """One deterministic, presentation-only marker per project's effective PM.

    ERP principal wins, including when its local contact is unresolved.  With no
    ERP principal, select the RP co-manager with the smallest *stable ID*, never
    by display name or database iteration order.  An absent identity is neutral.
    The exposed token is an opaque digest rather than an ERP or AppUser ID.
    """
    if managers is None:
        return None, None
    if managers.primary is not None:
        selected = managers.primary
    else:
        candidates = [
            (identity, co_manager)
            for co_manager in managers.co_managers
            if (identity := _stable_identity(co_manager)) is not None
        ]
        if not candidates:
            return None, None
        selected = min(candidates, key=lambda item: item[0])[1]
    identity = _stable_identity(selected)
    if identity is None:
        return None, None
    return sha256(identity.encode("utf-8")).hexdigest()[:12], selected.display_name
