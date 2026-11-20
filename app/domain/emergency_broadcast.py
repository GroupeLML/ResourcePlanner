from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import uuid


BROADCAST_ACTIVE = "ACTIVE"
BROADCAST_ENDED = "ENDED"
BROADCAST_VERSION = 1
TITLE_MAX_UTF16_UNITS = 160
MESSAGE_MAX_UTF16_UNITS = 4000


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def normalize_broadcast_text(value: str, *, field: str, max_units: int) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field} must not be blank")
    if _utf16_units(normalized) > max_units:
        raise ValueError(f"{field} exceeds {max_units} UTF-16 code units")
    return normalized


def normalize_command_id(value: str) -> str:
    try:
        return str(uuid.UUID(str(value or "").strip()))
    except (ValueError, AttributeError) as exc:
        raise ValueError("creation_command_id must be a UUID") from exc


def creation_payload_hash(*, title: str, message: str) -> str:
    payload = f"{BROADCAST_VERSION}\n{title}\n{message}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class EmergencyBroadcastRecord:
    id: str
    version: int
    title: str
    message: str
    created_by_user_id: str
    creation_command_id: str
    creation_payload_hash: str
    created_at: datetime
    activated_at: datetime
    state: str
    ended_at: datetime | None
    ended_by_user_id: str | None


@dataclass(frozen=True, slots=True)
class EmergencyBroadcastAcknowledgmentRecord:
    broadcast_id: str
    user_id: str
    acknowledged_at: datetime


@dataclass(frozen=True, slots=True)
class CreateEmergencyBroadcastCommand:
    actor_user_id: str
    creation_command_id: str
    title: str
    message: str


@dataclass(frozen=True, slots=True)
class EndEmergencyBroadcastCommand:
    actor_user_id: str
    broadcast_id: str
