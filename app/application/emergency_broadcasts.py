from __future__ import annotations

from datetime import datetime
from typing import Protocol, Sequence

from ..domain.emergency_broadcast import (
    CreateEmergencyBroadcastCommand,
    EmergencyBroadcastAcknowledgmentRecord,
    EmergencyBroadcastRecord,
    EndEmergencyBroadcastCommand,
    MESSAGE_MAX_UTF16_UNITS,
    TITLE_MAX_UTF16_UNITS,
    creation_payload_hash,
    normalize_broadcast_text,
    normalize_command_id,
)
from .errors import ApplicationValidationError


class EmergencyBroadcastRepositoryPort(Protocol):
    def create_active(
        self,
        *,
        actor_user_id: str,
        creation_command_id: str,
        title: str,
        message: str,
        payload_hash: str,
    ) -> EmergencyBroadcastRecord: ...

    def get_active(self) -> EmergencyBroadcastRecord | None: ...

    def get(self, broadcast_id: str) -> EmergencyBroadcastRecord | None: ...

    def end(
        self,
        *,
        broadcast_id: str,
        actor_user_id: str,
    ) -> EmergencyBroadcastRecord: ...

    def acknowledge(
        self,
        *,
        broadcast_id: str,
        user_id: str,
    ) -> EmergencyBroadcastAcknowledgmentRecord: ...

    def get_acknowledgment(
        self,
        *,
        broadcast_id: str,
        user_id: str,
    ) -> EmergencyBroadcastAcknowledgmentRecord | None: ...

    def list_expired_ended_ids(
        self,
        *,
        ended_before: datetime,
        limit: int = 100,
    ) -> Sequence[str]: ...


class EmergencyBroadcastService:
    def __init__(self, repository: EmergencyBroadcastRepositoryPort) -> None:
        self._repository = repository

    @staticmethod
    def _user_id(value: str, *, field: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ApplicationValidationError(
                f"{field} is required",
                code="emergency_broadcast_identity_required",
            )
        return normalized

    def create(
        self,
        command: CreateEmergencyBroadcastCommand,
    ) -> EmergencyBroadcastRecord:
        try:
            title = normalize_broadcast_text(
                command.title,
                field="title",
                max_units=TITLE_MAX_UTF16_UNITS,
            )
            message = normalize_broadcast_text(
                command.message,
                field="message",
                max_units=MESSAGE_MAX_UTF16_UNITS,
            )
            command_id = normalize_command_id(command.creation_command_id)
        except ValueError as exc:
            raise ApplicationValidationError(
                str(exc),
                code="emergency_broadcast_invalid",
            ) from exc
        actor_user_id = self._user_id(command.actor_user_id, field="actor_user_id")
        return self._repository.create_active(
            actor_user_id=actor_user_id,
            creation_command_id=command_id,
            title=title,
            message=message,
            payload_hash=creation_payload_hash(title=title, message=message),
        )

    def active(self) -> EmergencyBroadcastRecord | None:
        return self._repository.get_active()

    def get(self, broadcast_id: str) -> EmergencyBroadcastRecord | None:
        return self._repository.get(str(broadcast_id or "").strip())

    def end(
        self,
        command: EndEmergencyBroadcastCommand,
    ) -> EmergencyBroadcastRecord:
        broadcast_id = str(command.broadcast_id or "").strip()
        if not broadcast_id:
            raise ApplicationValidationError(
                "broadcast_id is required",
                code="emergency_broadcast_invalid",
            )
        actor_user_id = self._user_id(command.actor_user_id, field="actor_user_id")
        return self._repository.end(
            broadcast_id=broadcast_id,
            actor_user_id=actor_user_id,
        )

    def acknowledge(
        self,
        *,
        broadcast_id: str,
        authenticated_user_id: str,
    ) -> EmergencyBroadcastAcknowledgmentRecord:
        normalized_broadcast_id = str(broadcast_id or "").strip()
        if not normalized_broadcast_id:
            raise ApplicationValidationError(
                "broadcast_id is required",
                code="emergency_broadcast_invalid",
            )
        user_id = self._user_id(
            authenticated_user_id,
            field="authenticated_user_id",
        )
        return self._repository.acknowledge(
            broadcast_id=normalized_broadcast_id,
            user_id=user_id,
        )

    def acknowledgment(
        self,
        *,
        broadcast_id: str,
        authenticated_user_id: str,
    ) -> EmergencyBroadcastAcknowledgmentRecord | None:
        return self._repository.get_acknowledgment(
            broadcast_id=str(broadcast_id or "").strip(),
            user_id=self._user_id(
                authenticated_user_id,
                field="authenticated_user_id",
            ),
        )

    def expired_ended_ids(
        self,
        *,
        ended_before: datetime,
        limit: int = 100,
    ) -> tuple[str, ...]:
        bounded_limit = max(1, min(int(limit), 100))
        return tuple(
            self._repository.list_expired_ended_ids(
                ended_before=ended_before,
                limit=bounded_limit,
            )
        )
