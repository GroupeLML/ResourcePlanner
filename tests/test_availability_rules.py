from __future__ import annotations

import unittest
from datetime import date

from app.domain.availability_rules import (
    availability_hours_for_day,
    availability_state_for_day,
    has_standard_schedule,
    has_standard_schedule_in_window,
    outside_schedule_eligible_for_day,
    outside_standard_hours_decision_for_day,
)


MONDAY = date(2026, 8, 17)
TUESDAY = date(2026, 8, 18)
SATURDAY = date(2026, 8, 22)


def standard(
    resource: str = "R1",
    start: object = "08:00",
    end: object = "16:00",
    *,
    date_start: object = None,
    date_end: object = None,
) -> dict[str, object]:
    return {
        "Technicien": resource,
        "Type": "Horaire standard",
        "DateDebut": date_start,
        "DateFin": date_end,
        "JoursSemaine": "Lun,Mar,Mer,Jeu,Ven",
        "HeureDebut": start,
        "HeureFin": end,
        "Actif": "Oui",
    }


class AvailabilityRulesTests(unittest.TestCase):
    def test_resource_requires_explicit_standard_schedule(self) -> None:
        self.assertFalse(has_standard_schedule([], "R1"))
        self.assertEqual(availability_hours_for_day([], "R1", MONDAY), 0)
        self.assertFalse(outside_schedule_eligible_for_day([], "R1", MONDAY))

    def test_standard_schedule_returns_daily_hours(self) -> None:
        self.assertEqual(availability_hours_for_day([standard()], "R1", MONDAY), 8)
        self.assertEqual(availability_hours_for_day([standard()], "R1", SATURDAY), 0)

    def test_schedule_that_ended_before_window_is_not_schedulable(self) -> None:
        rows = [standard(date_start=date(2026, 1, 1), date_end=date(2026, 8, 16))]

        self.assertTrue(has_standard_schedule(rows, "R1"))
        self.assertFalse(
            has_standard_schedule_in_window(rows, "R1", MONDAY, SATURDAY)
        )
        self.assertEqual(availability_hours_for_day(rows, "R1", MONDAY), 0)
        self.assertFalse(outside_schedule_eligible_for_day(rows, "R1", SATURDAY))

    def test_schedule_overlapping_display_window_is_schedulable(self) -> None:
        rows = [standard(date_start=TUESDAY, date_end=date(2026, 9, 30))]

        self.assertTrue(
            has_standard_schedule_in_window(rows, "R1", MONDAY, SATURDAY)
        )
        self.assertFalse(
            has_standard_schedule_in_window(
                rows,
                "R1",
                date(2026, 8, 10),
                date(2026, 8, 16),
            )
        )
        self.assertEqual(availability_hours_for_day(rows, "R1", MONDAY), 0)
        self.assertEqual(availability_hours_for_day(rows, "R1", TUESDAY), 8)

    def test_excel_fraction_times_are_supported(self) -> None:
        rows = [standard(start=8 / 24, end=16 / 24)]
        self.assertAlmostEqual(availability_hours_for_day(rows, "R1", MONDAY), 8, places=6)

    def test_excel_fraction_times_match_historical_minute_rounding(self) -> None:
        rows = [standard(start=0.333333, end=0.666667)]
        self.assertEqual(availability_hours_for_day(rows, "R1", MONDAY), 8)

    def test_overnight_schedule_wraps_to_next_day(self) -> None:
        rows = [standard(start="22:00", end="06:00")]
        self.assertEqual(availability_hours_for_day(rows, "R1", MONDAY), 8)

    def test_global_holiday_blocks_standard_capacity_but_allows_overtime_slot(self) -> None:
        rows = [
            standard(),
            {
                "Type": "Jour férié",
                "DateDebut": TUESDAY,
                "DateFin": TUESDAY,
                "Actif": "Oui",
            },
        ]
        self.assertEqual(availability_hours_for_day(rows, "R1", TUESDAY), 0)
        self.assertTrue(outside_schedule_eligible_for_day(rows, "R1", TUESDAY))

    def test_weekend_without_standard_capacity_allows_overtime_slot(self) -> None:
        rows = [standard()]
        self.assertEqual(availability_hours_for_day(rows, "R1", SATURDAY), 0)
        self.assertTrue(outside_schedule_eligible_for_day(rows, "R1", SATURDAY))

    def test_resource_specific_holiday_does_not_block_other_resource(self) -> None:
        rows = [
            standard("R1"),
            standard("R2"),
            {
                "Technicien": "R1",
                "Type": "Jour férié",
                "DateDebut": MONDAY,
                "DateFin": MONDAY,
                "Actif": "Oui",
            },
        ]
        self.assertEqual(availability_hours_for_day(rows, "R1", MONDAY), 0)
        self.assertEqual(availability_hours_for_day(rows, "R2", MONDAY), 8)

    def test_vacation_blocks_only_matching_resource_and_excludes_overtime_slot(self) -> None:
        rows = [
            standard("R1"),
            standard("R2"),
            {
                "Technicien": "R1",
                "Type": "Vacances",
                "DateDebut": MONDAY,
                "DateFin": TUESDAY,
                "Actif": "Oui",
            },
        ]
        self.assertEqual(availability_hours_for_day(rows, "R1", MONDAY), 0)
        self.assertEqual(availability_hours_for_day(rows, "R2", MONDAY), 8)
        self.assertFalse(outside_schedule_eligible_for_day(rows, "R1", MONDAY))
        self.assertTrue(outside_schedule_eligible_for_day(rows, "R2", MONDAY))

    def test_standard_schedule_is_allowed_without_override(self) -> None:
        decision = outside_standard_hours_decision_for_day(
            [standard()],
            "R1",
            MONDAY,
            outside_standard_hours=False,
        )
        self.assertTrue(decision.allowed)
        self.assertFalse(decision.override_eligible)
        self.assertFalse(decision.override_applied)
        self.assertEqual(decision.state.hours, 8)

    def test_outside_standard_schedule_requires_explicit_override(self) -> None:
        blocked = outside_standard_hours_decision_for_day(
            [standard()],
            "R1",
            SATURDAY,
            outside_standard_hours=False,
        )
        allowed = outside_standard_hours_decision_for_day(
            [standard()],
            "R1",
            SATURDAY,
            outside_standard_hours=True,
        )
        self.assertEqual(blocked.state.reason, "Hors horaire standard")
        self.assertTrue(blocked.override_required)
        self.assertFalse(blocked.allowed)
        self.assertTrue(allowed.allowed)
        self.assertTrue(allowed.override_applied)

    def test_holiday_requires_explicit_override(self) -> None:
        rows = [
            standard(),
            {
                "Type": "Jour férié",
                "DateDebut": TUESDAY,
                "DateFin": TUESDAY,
                "Actif": "Oui",
            },
        ]
        blocked = outside_standard_hours_decision_for_day(
            rows,
            "R1",
            TUESDAY,
            outside_standard_hours=False,
        )
        allowed = outside_standard_hours_decision_for_day(
            rows,
            "R1",
            TUESDAY,
            outside_standard_hours=True,
        )
        self.assertEqual(blocked.state.reason, "Jour férié")
        self.assertTrue(blocked.override_required)
        self.assertFalse(blocked.allowed)
        self.assertTrue(allowed.allowed)
        self.assertTrue(allowed.override_applied)

    def test_vacation_is_never_overridable(self) -> None:
        rows = [
            standard(),
            {
                "Technicien": "R1",
                "Type": "Vacances",
                "DateDebut": MONDAY,
                "DateFin": MONDAY,
                "Actif": "Oui",
            },
        ]
        for explicit_override in (False, True):
            decision = outside_standard_hours_decision_for_day(
                rows,
                "R1",
                MONDAY,
                outside_standard_hours=explicit_override,
            )
            self.assertEqual(decision.state.reason, "Vacances")
            self.assertFalse(decision.override_eligible)
            self.assertFalse(decision.allowed)

    def test_missing_standard_schedule_is_never_overridable(self) -> None:
        for explicit_override in (False, True):
            decision = outside_standard_hours_decision_for_day(
                [],
                "R1",
                MONDAY,
                outside_standard_hours=explicit_override,
            )
            self.assertEqual(decision.state.reason, "Aucun horaire standard")
            self.assertFalse(decision.override_eligible)
            self.assertFalse(decision.allowed)

    def test_invalid_standard_schedule_is_never_overridable(self) -> None:
        rows = [standard(start=None, end=None)]
        self.assertEqual(
            availability_state_for_day(rows, "R1", MONDAY).reason,
            "Horaire standard invalide",
        )
        for explicit_override in (False, True):
            decision = outside_standard_hours_decision_for_day(
                rows,
                "R1",
                MONDAY,
                outside_standard_hours=explicit_override,
            )
            self.assertEqual(decision.state.reason, "Horaire standard invalide")
            self.assertFalse(decision.override_eligible)
            self.assertFalse(decision.allowed)

    def test_inactive_exception_is_ignored(self) -> None:
        rows = [
            standard(),
            {
                "Type": "Jour férié",
                "DateDebut": MONDAY,
                "DateFin": MONDAY,
                "Actif": "Non",
            },
        ]
        self.assertEqual(availability_hours_for_day(rows, "R1", MONDAY), 8)
        self.assertTrue(outside_schedule_eligible_for_day(rows, "R1", MONDAY))


if __name__ == "__main__":
    unittest.main()
