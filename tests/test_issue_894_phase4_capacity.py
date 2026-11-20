from __future__ import annotations

from datetime import datetime, timezone
import gc
import tracemalloc
import unittest

from app.domain.emergency_broadcast import EmergencyBroadcastAcknowledgmentRecord
from app.server.emergency_broadcast_runtime import (
    PresenceRegistry,
    _PositiveReceiptCache,
)


class _Clock:
    def __init__(self) -> None:
        self.value = 1000.0

    def monotonic(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class Issue894Phase4CapacityTests(unittest.TestCase):
    def test_presence_and_positive_receipt_caches_stay_under_16_mib_at_caps(self) -> None:
        clock = _Clock()
        tracemalloc.start()
        try:
            before, _peak = tracemalloc.get_traced_memory()
            presence = PresenceRegistry(
                monotonic=clock.monotonic,
                utc_now=lambda: datetime(2026, 11, 20, tzinfo=timezone.utc),
            )
            receipts = _PositiveReceiptCache(monotonic=clock.monotonic)

            for index in range(10_000):
                user_id = f"CAPACITY-{index:05d}"
                self.assertTrue(
                    presence.observe(
                        user_id,
                        display_name=f"Utilisateur {index:05d}",
                    )
                )
                receipts.put(
                    EmergencyBroadcastAcknowledgmentRecord(
                        broadcast_id="00000000-0000-4000-8000-000000000894",
                        user_id=user_id,
                        acknowledged_at=datetime(
                            2026,
                            11,
                            20,
                            tzinfo=timezone.utc,
                        ),
                    )
                )

            current, peak = tracemalloc.get_traced_memory()
            feature_current = current - before
            feature_peak = peak - before
            self.assertEqual(presence.snapshot().tracked_count, 10_000)
            self.assertEqual(len(receipts._values), 10_000)
            self.assertLessEqual(
                feature_current,
                16 * 1024 * 1024,
                f"feature caches retained {feature_current} bytes at caps",
            )
            self.assertLessEqual(
                feature_peak,
                16 * 1024 * 1024,
                f"feature cache construction peaked at {feature_peak} bytes",
            )

            clock.advance(16 * 60)
            self.assertEqual(presence.prune(), 10_000)
            self.assertEqual(receipts.prune(), 10_000)
            gc.collect()
            self.assertEqual(presence.snapshot().tracked_count, 0)
            self.assertEqual(len(receipts._values), 0)
        finally:
            tracemalloc.stop()


if __name__ == "__main__":
    unittest.main()
