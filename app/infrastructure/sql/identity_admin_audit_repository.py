from __future__ import annotations

import json
from typing import Mapping

from sqlalchemy.orm import Session

from .identity_models import IdentityAdminAudit


class SqlIdentityAdminAuditRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def record_event(
        self,
        *,
        actor_user_id: str,
        target_user_id: str,
        erp_user_id: str | None,
        action: str,
        old_state: Mapping[str, object],
        new_state: Mapping[str, object],
    ) -> None:
        self._session.add(
            IdentityAdminAudit(
                actor_user_id=str(actor_user_id).strip(),
                target_user_id=str(target_user_id).strip(),
                erp_user_id=str(erp_user_id).strip() if erp_user_id else None,
                action=str(action).strip(),
                old_state_json=json.dumps(
                    dict(old_state),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                new_state_json=json.dumps(
                    dict(new_state),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        )
        self._session.flush()
