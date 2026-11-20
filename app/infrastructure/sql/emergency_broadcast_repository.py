from __future__ import annotations

import random
import time
from datetime import datetime
from typing import Sequence

from sqlalchemy import delete, select, tuple_, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from ...application.emergency_broadcasts import EmergencyBroadcastRepositoryPort
from ...application.errors import (
    ApplicationConflictError,
    ApplicationNotFoundError,
    ApplicationUnavailableError,
)
from ...domain.emergency_broadcast import (
    BROADCAST_ACTIVE,
    BROADCAST_ENDED,
    EmergencyBroadcastAcknowledgmentRecord,
    EmergencyBroadcastRecord,
)
from .emergency_broadcast_models import (
    EmergencyBroadcast,
    EmergencyBroadcastAcknowledgment,
)
from .session import SqlSessionFactory


def _broadcast_record(row: EmergencyBroadcast) -> EmergencyBroadcastRecord:
    return EmergencyBroadcastRecord(
        id=row.id,
        version=row.version,
        title=row.title,
        message=row.message,
        created_by_user_id=row.created_by_user_id,
        creation_command_id=row.creation_command_id,
        creation_payload_hash=row.creation_payload_hash,
        created_at=row.created_at,
        activated_at=row.activated_at,
        state=row.state,
        ended_at=row.ended_at,
        ended_by_user_id=row.ended_by_user_id,
    )


def _ack_record(
    row: EmergencyBroadcastAcknowledgment,
) -> EmergencyBroadcastAcknowledgmentRecord:
    return EmergencyBroadcastAcknowledgmentRecord(
        broadcast_id=row.broadcast_id,
        user_id=row.user_id,
        acknowledged_at=row.acknowledged_at,
    )


def _error_text(exc: BaseException) -> str:
    parts: list[str] = []
    current: BaseException | None = exc
    while current is not None:
        parts.append(str(current))
        current = getattr(current, "orig", None)
        if current is exc:
            break
    return " ".join(parts).casefold()


def _is_duplicate_integrity(exc: IntegrityError) -> bool:
    text_value = _error_text(exc)
    return (
        "2601" in text_value
        or "2627" in text_value
        or "unique constraint failed" in text_value
        or "duplicate key" in text_value
    )


def _is_deadlock(exc: DBAPIError) -> bool:
    return "1205" in _error_text(exc) or "deadlock victim" in _error_text(exc)


class SqlEmergencyBroadcastRepository(EmergencyBroadcastRepositoryPort):
    """Committed, feature-owned persistence operations for emergency broadcasts."""

    def __init__(self, factory: SqlSessionFactory) -> None:
        self._factory = factory

    def _read_command(
        self,
        *,
        actor_user_id: str,
        creation_command_id: str,
    ) -> EmergencyBroadcast | None:
        with self._factory() as session:
            return session.scalar(
                select(EmergencyBroadcast).where(
                    EmergencyBroadcast.created_by_user_id == actor_user_id,
                    EmergencyBroadcast.creation_command_id == creation_command_id,
                )
            )

    def _read_active(self) -> EmergencyBroadcast | None:
        with self._factory() as session:
            return session.scalar(
                select(EmergencyBroadcast).where(
                    EmergencyBroadcast.state == BROADCAST_ACTIVE
                )
            )

    def create_active(
        self,
        *,
        actor_user_id: str,
        creation_command_id: str,
        title: str,
        message: str,
        payload_hash: str,
    ) -> EmergencyBroadcastRecord:
        row = EmergencyBroadcast(
            title=title,
            message=message,
            created_by_user_id=actor_user_id,
            creation_command_id=creation_command_id,
            creation_payload_hash=payload_hash,
            state=BROADCAST_ACTIVE,
        )
        try:
            with self._factory() as session:
                session.add(row)
                session.commit()
                session.refresh(row)
                return _broadcast_record(row)
        except IntegrityError as exc:
            if not _is_duplicate_integrity(exc):
                raise
            existing = self._read_command(
                actor_user_id=actor_user_id,
                creation_command_id=creation_command_id,
            )
            if existing is not None:
                if existing.creation_payload_hash != payload_hash:
                    raise ApplicationConflictError(
                        "Creation command was already used with different content.",
                        code="emergency_broadcast_idempotency_conflict",
                        context={"broadcast_id": existing.id},
                    ) from exc
                return _broadcast_record(existing)
            active = self._read_active()
            if active is not None:
                raise ApplicationConflictError(
                    "An emergency broadcast is already active.",
                    code="emergency_broadcast_already_active",
                    context={"broadcast_id": active.id},
                ) from exc
            raise

    def get_active(self) -> EmergencyBroadcastRecord | None:
        row = self._read_active()
        return _broadcast_record(row) if row is not None else None

    def get(self, broadcast_id: str) -> EmergencyBroadcastRecord | None:
        with self._factory() as session:
            row = session.get(EmergencyBroadcast, broadcast_id)
            return _broadcast_record(row) if row is not None else None

    def end(
        self,
        *,
        broadcast_id: str,
        actor_user_id: str,
    ) -> EmergencyBroadcastRecord:
        with self._factory() as session:
            result = session.execute(
                update(EmergencyBroadcast)
                .where(
                    EmergencyBroadcast.id == broadcast_id,
                    EmergencyBroadcast.state == BROADCAST_ACTIVE,
                )
                .values(
                    state=BROADCAST_ENDED,
                    ended_at=datetime.now().astimezone(),
                    ended_by_user_id=actor_user_id,
                )
            )
            session.commit()
            changed = bool(result.rowcount)
        current = self.get(broadcast_id)
        if current is None:
            raise ApplicationNotFoundError(
                "Emergency broadcast was not found.",
                code="emergency_broadcast_not_found",
                context={"broadcast_id": broadcast_id},
            )
        if changed or current.state == BROADCAST_ENDED:
            return current
        raise ApplicationConflictError(
            "Emergency broadcast could not be ended.",
            code="emergency_broadcast_end_conflict",
            context={"broadcast_id": broadcast_id},
        )

    def _read_ack(
        self,
        *,
        broadcast_id: str,
        user_id: str,
    ) -> EmergencyBroadcastAcknowledgment | None:
        with self._factory() as session:
            return session.get(
                EmergencyBroadcastAcknowledgment,
                (broadcast_id, user_id),
            )

    def acknowledge(
        self,
        *,
        broadcast_id: str,
        user_id: str,
    ) -> EmergencyBroadcastAcknowledgmentRecord:
        for attempt in range(3):
            row = EmergencyBroadcastAcknowledgment(
                broadcast_id=broadcast_id,
                user_id=user_id,
            )
            try:
                with self._factory() as session:
                    session.add(row)
                    session.commit()
                    session.refresh(row)
                    return _ack_record(row)
            except IntegrityError as exc:
                if _is_duplicate_integrity(exc):
                    canonical = self._read_ack(
                        broadcast_id=broadcast_id,
                        user_id=user_id,
                    )
                    if canonical is not None:
                        return _ack_record(canonical)
                    if self.get(broadcast_id) is None:
                        raise ApplicationNotFoundError(
                            "Emergency broadcast was not found.",
                            code="emergency_broadcast_not_found",
                            context={"broadcast_id": broadcast_id},
                        ) from exc
                raise
            except DBAPIError as exc:
                if not _is_deadlock(exc) or attempt >= 2:
                    raise ApplicationUnavailableError(
                        "Emergency broadcast acknowledgment is temporarily unavailable.",
                        code="emergency_broadcast_acknowledgment_unavailable",
                        context={"broadcast_id": broadcast_id},
                    ) from exc
                time.sleep(random.uniform(0.025, 0.1))
        raise AssertionError("unreachable")

    def get_acknowledgment(
        self,
        *,
        broadcast_id: str,
        user_id: str,
    ) -> EmergencyBroadcastAcknowledgmentRecord | None:
        row = self._read_ack(broadcast_id=broadcast_id, user_id=user_id)
        return _ack_record(row) if row is not None else None

    def list_expired_ended_ids(
        self,
        *,
        ended_before: datetime,
        limit: int = 100,
    ) -> Sequence[str]:
        bounded = max(1, min(int(limit), 100))
        with self._factory() as session:
            return tuple(
                session.scalars(
                    select(EmergencyBroadcast.id)
                    .where(
                        EmergencyBroadcast.state == BROADCAST_ENDED,
                        EmergencyBroadcast.ended_at.is_not(None),
                        EmergencyBroadcast.ended_at < ended_before,
                    )
                    .order_by(EmergencyBroadcast.ended_at, EmergencyBroadcast.id)
                    .limit(bounded)
                )
            )

    def delete_acknowledgments_for_expired(
        self,
        *,
        broadcast_ids: Sequence[str],
        limit: int = 1000,
    ) -> int:
        ids = tuple(dict.fromkeys(str(value) for value in broadcast_ids if value))
        if not ids:
            return 0
        bounded = max(1, min(int(limit), 1000))
        with self._factory() as session:
            keys = tuple(
                session.execute(
                    select(
                        EmergencyBroadcastAcknowledgment.broadcast_id,
                        EmergencyBroadcastAcknowledgment.user_id,
                    )
                    .where(
                        EmergencyBroadcastAcknowledgment.broadcast_id.in_(ids)
                    )
                    .limit(bounded)
                )
            )
            if not keys:
                return 0
            result = session.execute(
                delete(EmergencyBroadcastAcknowledgment).where(
                    tuple_(
                        EmergencyBroadcastAcknowledgment.broadcast_id,
                        EmergencyBroadcastAcknowledgment.user_id,
                    ).in_(keys)
                )
            )
            session.commit()
            return int(result.rowcount or 0)
