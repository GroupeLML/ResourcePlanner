from __future__ import annotations

from datetime import date
import unittest

from app.application.commands import QuickShiftCreateCommand
from app.application.errors import ApplicationValidationError
from app.application.quick_shift_service import QuickShiftService
from app.segment_repository import (
    QUICK_SHIFT_ORIGIN,
    SEGMENT_HEADERS,
    SEGMENT_ORIGIN_FIELD,
    ensure_segment_fields,
)


class _Segments:
    def __init__(self) -> None:
        self.created: dict[str, object] = {}
        self.updated: list[tuple[str, dict[str, object]]] = []

    def list(self, *, include_cancelled=True):
        return ()

    def get(self, segment_id):
        return None

    def create(self, values):
        self.created.update(dict(values))
        return "SEG-2026-0001"

    def update(self, segment_id, updates):
        self.updated.append((segment_id, dict(updates)))


class _Allocations:
    def __init__(self, failure: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self.failure = failure

    def create_manual(self, segment, technician, day, hours, overtime=False, note=""):
        self.calls.append((segment, technician, day, hours, overtime, note))
        if self.failure is not None:
            raise self.failure
        return "MAN-001"

    def update_manual(self, *args, **kwargs):
        raise AssertionError("not used")

    def release_manual(self, *args, **kwargs):
        raise AssertionError("not used")

    def delete_manual(self, *args, **kwargs):
        raise AssertionError("not used")

    def assign_segment(self, *args, **kwargs):
        raise AssertionError("not used")


class QuickShiftServiceTests(unittest.TestCase):
    def test_typed_create_builds_ad_hoc_segment_then_locked_shift(self) -> None:
        segments = _Segments()
        allocations = _Allocations()
        service = QuickShiftService(segments, allocations)

        result = service.create_command(
            QuickShiftCreateCommand(
                project_number="5094",
                project_name="Projet test",
                technician="Mathieu",
                day=date(2026, 8, 26),
                hours=7.5,
                outside_standard_hours=True,
                note="Intervention imprévue",
                description="Dépannage",
            )
        )

        self.assertEqual(result.segment_id, "SEG-2026-0001")
        self.assertEqual(result.allocation_id, "MAN-001")
        self.assertIsNone(segments.created["NoDemande"])
        self.assertEqual(segments.created["NumeroProjet"], "5094")
        self.assertEqual(segments.created["NomProjet"], "Projet test")
        self.assertEqual(segments.created["Technicien"], "Mathieu")
        self.assertEqual(segments.created["DateDebut"], date(2026, 8, 26))
        self.assertEqual(segments.created["DateFin"], date(2026, 8, 26))
        self.assertEqual(segments.created["HeuresPrevues"], 7.5)
        self.assertEqual(segments.created["TypePlanification"], "Fixe")
        self.assertEqual(segments.created[SEGMENT_ORIGIN_FIELD], QUICK_SHIFT_ORIGIN)
        self.assertEqual(
            allocations.calls,
            [
                (
                    "SEG-2026-0001",
                    "Mathieu",
                    date(2026, 8, 26),
                    7.5,
                    True,
                    "Intervention imprévue",
                )
            ],
        )
        self.assertEqual(segments.updated, [])

    def test_legacy_create_normalizes_string_values_before_workflow(self) -> None:
        segments = _Segments()
        allocations = _Allocations()
        result = QuickShiftService(segments, allocations).create(
            project_number="5094",
            technician="Mathieu",
            day_value="2026-08-26",
            hours_value="7,5",
        )

        self.assertEqual(result.segment_id, "SEG-2026-0001")
        self.assertEqual(allocations.calls[0][2], date(2026, 8, 26))
        self.assertEqual(allocations.calls[0][3], 7.5)

    def test_create_cancels_generated_segment_when_shift_creation_fails(self) -> None:
        segments = _Segments()
        allocations = _Allocations(ValueError("hors horaire requis"))
        service = QuickShiftService(segments, allocations)

        with self.assertRaises(ApplicationValidationError) as raised:
            service.create(
                project_number="5094",
                technician="Mathieu",
                day_value="2026-08-30",
                hours_value=8,
            )

        self.assertEqual(str(raised.exception), "hors horaire requis")
        self.assertEqual(raised.exception.code, "quick_shift_allocation_create_invalid")
        self.assertEqual(segments.updated, [("SEG-2026-0001", {"Statut": "Annulé"})])

    def test_create_rejects_invalid_required_values_before_persistence(self) -> None:
        segments = _Segments()
        allocations = _Allocations()
        service = QuickShiftService(segments, allocations)

        for kwargs in (
            dict(project_number="", technician="Mathieu", day_value="2026-08-26", hours_value=8),
            dict(project_number="5094", technician="", day_value="2026-08-26", hours_value=8),
            dict(project_number="5094", technician="Mathieu", day_value=None, hours_value=8),
            dict(project_number="5094", technician="Mathieu", day_value="2026-08-26", hours_value=0),
        ):
            with self.assertRaises(ApplicationValidationError):
                service.create(**kwargs)

        self.assertEqual(segments.created, {})
        self.assertEqual(allocations.calls, [])


class SegmentFieldExtensionTests(unittest.TestCase):
    def test_optional_field_is_appended_without_reordering_existing_headers(self) -> None:
        original = list(SEGMENT_HEADERS)

        class FakeRepository:
            def __init__(self) -> None:
                self.headers: list[str] = []
                self.save_count = 0

            def _ensure_sheet_table(self, _sheet: str, headers: list[str], _table: str) -> None:
                self.headers = list(headers)

            def save(self) -> None:
                self.save_count += 1

        repo = FakeRepository()
        try:
            ensure_segment_fields(repo, [SEGMENT_ORIGIN_FIELD])  # type: ignore[arg-type]
            ensure_segment_fields(repo, [SEGMENT_ORIGIN_FIELD])  # type: ignore[arg-type]

            self.assertEqual(SEGMENT_HEADERS[:-1], original)
            self.assertEqual(SEGMENT_HEADERS[-1], SEGMENT_ORIGIN_FIELD)
            self.assertEqual(SEGMENT_HEADERS.count(SEGMENT_ORIGIN_FIELD), 1)
            self.assertEqual(repo.headers, SEGMENT_HEADERS)
        finally:
            SEGMENT_HEADERS[:] = original


if __name__ == "__main__":
    unittest.main()
