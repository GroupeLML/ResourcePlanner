from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import threading
import time
from typing import Awaitable, Callable, Generic, TypeVar
from uuid import uuid4

from ..application.errors import ApplicationUnavailableError
from ..application.emergency_broadcasts import EmergencyBroadcastService
from ..domain.emergency_broadcast import (
    EmergencyBroadcastAcknowledgmentRecord,
    EmergencyBroadcastRecord,
)
from ..infrastructure.sql.emergency_broadcast_repository import (
    SqlEmergencyBroadcastRepository,
)
from ..infrastructure.sql.session import SqlSessionFactory


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class PresenceObservation:
    user_id: str
    last_seen_at: datetime
    active: bool


@dataclass(frozen=True, slots=True)
class PresenceSnapshot:
    generated_at: datetime
    observation_epoch: str
    warming: bool
    complete: bool
    tracked_count: int
    active_count: int
    observations: tuple[PresenceObservation, ...]


@dataclass(frozen=True, slots=True)
class EmergencyRuntimeDiagnostics:
    status_requests: int
    status_overloads: int
    status_errors: int
    acknowledgment_successes: int
    acknowledgment_errors: int
    active_cache_hits: int
    active_cache_refreshes: int
    active_cache_errors: int
    receipt_cache_hits: int
    cleanup_receipts_deleted: int
    cleanup_broadcasts_deleted: int
    cleanup_errors: int


class PresenceRegistry:
    ACTIVE_SECONDS = 60.0
    RETENTION_SECONDS = 15.0 * 60.0
    CAPACITY = 10_000
    INCOMPLETE_HOLD_SECONDS = 60.0

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        self._monotonic = monotonic
        self._utc_now = utc_now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._observed: dict[str, tuple[float, datetime]] = {}
        self._started_monotonic = monotonic()
        self._observation_epoch = str(uuid4())
        self._last_overflow_monotonic: float | None = None

    @property
    def observation_epoch(self) -> str:
        return self._observation_epoch

    def _prune_locked(self, now_mono: float) -> int:
        expired = [
            user_id
            for user_id, (seen_mono, _seen_at) in self._observed.items()
            if now_mono - seen_mono > self.RETENTION_SECONDS
        ]
        for user_id in expired:
            self._observed.pop(user_id, None)
        return len(expired)

    def observe(self, user_id: str) -> bool:
        value = str(user_id or "").strip()
        if not value:
            return False
        now_mono = self._monotonic()
        now_at = self._utc_now()
        with self._lock:
            existing = self._observed.get(value)
            if existing is not None:
                self._observed[value] = (max(existing[0], now_mono), max(existing[1], now_at))
                return True
            if len(self._observed) >= self.CAPACITY:
                self._prune_locked(now_mono)
            if len(self._observed) >= self.CAPACITY:
                inactive = sorted(
                    (
                        (seen_mono, user_id_value)
                        for user_id_value, (seen_mono, _seen_at) in self._observed.items()
                        if now_mono - seen_mono > self.ACTIVE_SECONDS
                    )
                )
                if inactive:
                    self._observed.pop(inactive[0][1], None)
            if len(self._observed) >= self.CAPACITY:
                self._last_overflow_monotonic = now_mono
                return False
            self._observed[value] = (now_mono, now_at)
            return True

    def prune(self) -> int:
        now_mono = self._monotonic()
        with self._lock:
            return self._prune_locked(now_mono)

    def snapshot(self) -> PresenceSnapshot:
        now_mono = self._monotonic()
        now_at = self._utc_now()
        with self._lock:
            self._prune_locked(now_mono)
            warming = now_mono - self._started_monotonic < self.ACTIVE_SECONDS
            incomplete = (
                self._last_overflow_monotonic is not None
                and now_mono - self._last_overflow_monotonic
                < self.INCOMPLETE_HOLD_SECONDS
            )
            observations = tuple(
                PresenceObservation(
                    user_id=user_id,
                    last_seen_at=seen_at,
                    active=now_mono - seen_mono <= self.ACTIVE_SECONDS,
                )
                for user_id, (seen_mono, seen_at) in sorted(self._observed.items())
            )
        return PresenceSnapshot(
            generated_at=now_at,
            observation_epoch=self._observation_epoch,
            warming=warming,
            complete=not warming and not incomplete,
            tracked_count=len(observations),
            active_count=sum(1 for item in observations if item.active),
            observations=observations,
        )


class _ActiveBroadcastCache:
    TTL_SECONDS = 1.0

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._condition = threading.Condition()
        self._value: EmergencyBroadcastRecord | None = None
        self._expires_at = 0.0
        self._loaded = False
        self._loading = False
        self._generation = 0

    def get(
        self,
        loader: Callable[[], EmergencyBroadcastRecord | None],
    ) -> tuple[EmergencyBroadcastRecord | None, bool]:
        with self._condition:
            now = self._monotonic()
            if self._loaded and now < self._expires_at:
                return self._value, True
            generation = self._generation
            if self._loading:
                self._condition.wait(timeout=self.TTL_SECONDS)
                now = self._monotonic()
                if self._loaded and now < self._expires_at:
                    return self._value, True
            self._loading = True
        try:
            value = loader()
        except Exception:
            with self._condition:
                self._loading = False
                self._condition.notify_all()
            raise
        with self._condition:
            if self._generation == generation:
                self._value = value
                self._loaded = True
                self._expires_at = self._monotonic() + self.TTL_SECONDS
            self._loading = False
            self._condition.notify_all()
            return self._value if self._generation == generation else value, False

    def set_after_commit(self, value: EmergencyBroadcastRecord | None) -> None:
        with self._condition:
            self._generation += 1
            self._value = value
            self._loaded = True
            self._expires_at = self._monotonic() + self.TTL_SECONDS
            self._condition.notify_all()

    def invalidate(self) -> None:
        with self._condition:
            self._generation += 1
            self._loaded = False
            self._expires_at = 0.0
            self._condition.notify_all()


class _PositiveReceiptCache:
    CAPACITY = 10_000
    TTL_SECONDS = 15.0 * 60.0

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._values: OrderedDict[
            tuple[str, str],
            tuple[float, EmergencyBroadcastAcknowledgmentRecord],
        ] = OrderedDict()

    def get(
        self,
        broadcast_id: str,
        user_id: str,
    ) -> EmergencyBroadcastAcknowledgmentRecord | None:
        key = (broadcast_id, user_id)
        now = self._monotonic()
        with self._lock:
            item = self._values.get(key)
            if item is None:
                return None
            expires_at, record = item
            if now >= expires_at:
                self._values.pop(key, None)
                return None
            self._values.move_to_end(key)
            return record

    def put(self, record: EmergencyBroadcastAcknowledgmentRecord) -> None:
        key = (record.broadcast_id, record.user_id)
        with self._lock:
            self._values[key] = (
                self._monotonic() + self.TTL_SECONDS,
                record,
            )
            self._values.move_to_end(key)
            while len(self._values) > self.CAPACITY:
                self._values.popitem(last=False)

    def prune(self) -> int:
        now = self._monotonic()
        with self._lock:
            expired = [
                key
                for key, (expires_at, _record) in self._values.items()
                if now >= expires_at
            ]
            for key in expired:
                self._values.pop(key, None)
            return len(expired)


class AsyncAdmission(Generic[T]):
    def __init__(
        self,
        *,
        max_in_flight: int,
        max_waiting: int,
        acquire_timeout_seconds: float,
    ) -> None:
        self._semaphore = asyncio.Semaphore(max_in_flight)
        self._max_waiting = max_waiting
        self._acquire_timeout_seconds = acquire_timeout_seconds
        self._state_lock = asyncio.Lock()
        self._waiting = 0
        self._in_flight = 0

    async def run(self, work: Callable[[], Awaitable[T]]) -> T:
        async with self._state_lock:
            if self._waiting >= self._max_waiting:
                raise ApplicationUnavailableError(
                    "Emergency runtime capacity is temporarily saturated.",
                    code="emergency_runtime_overloaded",
                )
            self._waiting += 1
        acquired = False
        try:
            try:
                await asyncio.wait_for(
                    self._semaphore.acquire(),
                    timeout=self._acquire_timeout_seconds,
                )
                acquired = True
            except TimeoutError as exc:
                raise ApplicationUnavailableError(
                    "Emergency runtime capacity is temporarily saturated.",
                    code="emergency_runtime_overloaded",
                ) from exc
            async with self._state_lock:
                self._in_flight += 1
            return await work()
        finally:
            async with self._state_lock:
                self._waiting = max(0, self._waiting - 1)
                if acquired:
                    self._in_flight = max(0, self._in_flight - 1)
            if acquired:
                self._semaphore.release()

    async def counts(self) -> tuple[int, int]:
        async with self._state_lock:
            return self._waiting, self._in_flight


class EmergencyBroadcastRuntime:
    RETENTION_DAYS = 90

    def __init__(self, factory: SqlSessionFactory) -> None:
        self.factory = factory
        self.repository = SqlEmergencyBroadcastRepository(factory)
        self.service = EmergencyBroadcastService(self.repository)
        self.presence = PresenceRegistry()
        self._active_cache = _ActiveBroadcastCache()
        self._receipt_cache = _PositiveReceiptCache()
        self.status_admission: AsyncAdmission[object] = AsyncAdmission(
            max_in_flight=32,
            max_waiting=64,
            acquire_timeout_seconds=0.25,
        )
        self.command_admission: AsyncAdmission[object] = AsyncAdmission(
            max_in_flight=32,
            max_waiting=512,
            acquire_timeout_seconds=5.0,
        )
        self._metrics_lock = threading.Lock()
        self._metrics = {
            "status_requests": 0,
            "status_overloads": 0,
            "status_errors": 0,
            "acknowledgment_successes": 0,
            "acknowledgment_errors": 0,
            "active_cache_hits": 0,
            "active_cache_refreshes": 0,
            "active_cache_errors": 0,
            "receipt_cache_hits": 0,
            "cleanup_receipts_deleted": 0,
            "cleanup_broadcasts_deleted": 0,
            "cleanup_errors": 0,
        }

    def _increment(self, key: str, amount: int = 1) -> None:
        with self._metrics_lock:
            self._metrics[key] += amount

    def diagnostics(self) -> EmergencyRuntimeDiagnostics:
        with self._metrics_lock:
            return EmergencyRuntimeDiagnostics(**self._metrics)

    def active_broadcast(self) -> EmergencyBroadcastRecord | None:
        try:
            value, hit = self._active_cache.get(self.service.active)
        except Exception as exc:
            self._increment("active_cache_errors")
            if isinstance(exc, ApplicationUnavailableError):
                raise
            raise ApplicationUnavailableError(
                "Emergency broadcast status is temporarily unavailable.",
                code="emergency_broadcast_status_unavailable",
            ) from exc
        self._increment("active_cache_hits" if hit else "active_cache_refreshes")
        return value

    def acknowledgment(
        self,
        *,
        broadcast_id: str,
        user_id: str,
    ) -> EmergencyBroadcastAcknowledgmentRecord | None:
        cached = self._receipt_cache.get(broadcast_id, user_id)
        if cached is not None:
            self._increment("receipt_cache_hits")
            return cached
        try:
            receipt = self.service.acknowledgment(
                broadcast_id=broadcast_id,
                authenticated_user_id=user_id,
            )
        except Exception as exc:
            if isinstance(exc, ApplicationUnavailableError):
                raise
            raise ApplicationUnavailableError(
                "Emergency acknowledgment state is temporarily unavailable.",
                code="emergency_broadcast_status_unavailable",
            ) from exc
        if receipt is not None:
            self._receipt_cache.put(receipt)
        return receipt

    def acknowledge(
        self,
        *,
        broadcast_id: str,
        user_id: str,
    ) -> EmergencyBroadcastAcknowledgmentRecord:
        try:
            receipt = self.service.acknowledge(
                broadcast_id=broadcast_id,
                authenticated_user_id=user_id,
            )
        except Exception as exc:
            self._increment("acknowledgment_errors")
            if hasattr(exc, "code"):
                raise
            raise ApplicationUnavailableError(
                "Emergency acknowledgment is temporarily unavailable.",
                code="emergency_broadcast_acknowledgment_unavailable",
            ) from exc
        self._receipt_cache.put(receipt)
        self._increment("acknowledgment_successes")
        return receipt

    def create(self, command):
        try:
            record = self.service.create(command)
        except Exception as exc:
            if hasattr(exc, "code"):
                raise
            raise ApplicationUnavailableError(
                "Emergency broadcast creation is temporarily unavailable.",
                code="emergency_broadcast_creation_unavailable",
            ) from exc
        self._active_cache.set_after_commit(record)
        return record

    def end(self, command):
        try:
            record = self.service.end(command)
        except Exception as exc:
            if hasattr(exc, "code"):
                raise
            raise ApplicationUnavailableError(
                "Emergency broadcast ending is temporarily unavailable.",
                code="emergency_broadcast_end_unavailable",
            ) from exc
        self._active_cache.set_after_commit(None)
        return record

    def prune_memory(self) -> tuple[int, int]:
        return self.presence.prune(), self._receipt_cache.prune()

    def cleanup_retention(self) -> tuple[int, int]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.RETENTION_DAYS)
        try:
            receipt_count = self.repository.delete_acknowledgments_for_expired(
                ended_before=cutoff,
                limit=1000,
            )
            broadcast_count = self.repository.delete_empty_expired_broadcasts(
                ended_before=cutoff,
                limit=100,
            )
        except Exception:
            self._increment("cleanup_errors")
            raise
        self._increment("cleanup_receipts_deleted", receipt_count)
        self._increment("cleanup_broadcasts_deleted", broadcast_count)
        return receipt_count, broadcast_count
