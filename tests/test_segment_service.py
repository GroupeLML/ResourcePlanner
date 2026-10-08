from __future__ import annotations

from datetime import date
from pathlib import Path
import unittest

from app.application.commands import SegmentUpdateCommand
from app.application.errors import ApplicationNotFoundError, ApplicationValidationError
from app.application.read_models import SegmentReadModel
from app.application.segment_service import SegmentService


class _Segments:
    def __init__(self, record: SegmentReadModel | None = None) -> None:
        self.events: list[object] = []
        self.record = record or SegmentReadModel(
            segment_id="SEG-1",
            demand_number="DMO-1",
            project_number="P-1",
            project_name="Projet",
            resource_name=None,
            start_date=date(2026, 8, 25),
            end_date=date(2026, 8, 30),
            planned_hours=8,
            status="À assigner",
        )

    def list(self, *, include_cancelled=True):
        return (self.record,) if self.record is not None else ()

    def get(self, segment_id):
        if self.record is not None and self.record.segment_id == segment_id:
            return self.record
        return None

    def create(self, values):
        self.events.append(("create", dict(values)))
        return "SEG-2026-0001"

    def update(self, identifier, values):
        self.events.append(("update", identifier, dict(values)))


class _Planning:
    def __init__(self, events: list[object]) -> None:
        self.events = events

    def rebuild(self):
        self.events.append(("rebuild",))
        return {"allocated_hours": 8.0, "unallocated_hours": 0.0}


class SegmentServiceTests(unittest.TestCase):
    def test_create_persists_typed_payload_then_rebuilds_once(self) -> None:
        segments = _Segments()
        planning = _Planning(segments.events)
        service = SegmentService(segments, planning)

        identifier, summary = service.create(
            {
                "NoDemande": "DMO-1",
                "DateDebut": "2026-08-25",
                "DateFin": "2026-08-26",
                "HeuresPrevues": 8,
                "CompetenceRequise": "Programmation",
            }
        )

        self.assertEqual(identifier, "SEG-2026-0001")
        self.assertEqual(summary["allocated_hours"], 8.0)
        create = segments.events[0]
        self.assertEqual(create[0], "create")
        self.assertEqual(create[1]["NoDemande"], "DMO-1")
        self.assertEqual(create[1]["DateDebut"], date(2026, 8, 25))
        self.assertEqual(create[1]["DateFin"], date(2026, 8, 26))
        self.assertEqual(create[1]["HeuresPrevues"], 8.0)
        self.assertEqual(segments.events[-1], ("rebuild",))

    def test_update_command_and_cancel_rebuild_after_mutation(self) -> None:
        segments = _Segments()
        planning = _Planning(segments.events)
        service = SegmentService(segments, planning)

        service.update_command(
            SegmentUpdateCommand(segment_id="SEG-1", planned_hours=6)
        )
        service.cancel("SEG-1")

        self.assertEqual(
            segments.events,
            [
                ("update", "SEG-1", {"HeuresPrevues": 6}),
                ("rebuild",),
                ("update", "SEG-1", {"Statut": "Annulé"}),
                ("rebuild",),
            ],
        )

    def test_update_validates_window_against_existing_read_model(self) -> None:
        segments = _Segments()
        service = SegmentService(segments, _Planning(segments.events))

        with self.assertRaises(ApplicationValidationError) as raised:
            service.update_command(
                SegmentUpdateCommand(
                    segment_id="SEG-1",
                    start_date=date(2026, 9, 1),
                )
            )

        self.assertEqual(raised.exception.code, "segment_date_window_invalid")
        self.assertEqual(segments.events, [])

    def test_update_without_demand_requires_canonical_adhoc_origin(self) -> None:
        orphan_request = SegmentReadModel(
            segment_id="SEG-ORPHAN",
            demand_number=None,
            project_number="P-1",
            project_name="Projet",
            resource_name=None,
            start_date=date(2026, 8, 25),
            end_date=date(2026, 8, 25),
            planned_hours=8,
            status="À assigner",
            origin="REQUEST",
        )
        segments = _Segments(record=orphan_request)
        service = SegmentService(segments, _Planning(segments.events))

        with self.assertRaises(ApplicationValidationError) as raised:
            service.update_command(
                SegmentUpdateCommand(
                    segment_id="SEG-ORPHAN",
                    end_date=date(2026, 8, 29),
                )
            )

        self.assertEqual(raised.exception.code, "segment_autonomous_origin_required")
        self.assertEqual(segments.events, [])

    def test_update_without_demand_accepts_canonical_quick_shift_origin(self) -> None:
        quick_shift = SegmentReadModel(
            segment_id="SEG-QUICK",
            demand_number=None,
            project_number="P-1",
            project_name="Projet",
            resource_name="Alice",
            start_date=date(2026, 8, 25),
            end_date=date(2026, 8, 25),
            planned_hours=8,
            status="Planifié",
            origin="QUICK_SHIFT",
        )
        segments = _Segments(record=quick_shift)
        service = SegmentService(segments, _Planning(segments.events))

        service.update_command(
            SegmentUpdateCommand(
                segment_id="SEG-QUICK",
                end_date=date(2026, 8, 29),
            )
        )

        self.assertEqual(
            segments.events,
            [
                ("update", "SEG-QUICK", {"DateFin": date(2026, 8, 29)}),
                ("rebuild",),
            ],
        )

    def test_unknown_segment_is_structured_not_found(self) -> None:
        segments = _Segments(record=None)
        segments.record = None
        service = SegmentService(segments, _Planning(segments.events))

        with self.assertRaises(ApplicationNotFoundError) as raised:
            service.update("SEG-MISSING", {"HeuresPrevues": 6})

        self.assertEqual(raised.exception.code, "segment_not_found")
        self.assertEqual(raised.exception.context["segment_id"], "SEG-MISSING")
        self.assertEqual(segments.events, [])

    def test_empty_identifiers_are_structured_validation_errors(self) -> None:
        segments = _Segments()
        service = SegmentService(segments, _Planning(segments.events))
        with self.assertRaises(ApplicationValidationError):
            service.create(
                {
                    "NoDemande": "",
                    "DateDebut": "2026-08-25",
                    "HeuresPrevues": 8,
                }
            )
        with self.assertRaises(ApplicationValidationError):
            service.update("", {})
        self.assertEqual(segments.events, [])

    def test_application_service_has_only_port_command_and_error_dependencies(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "application"
            / "segment_service.py"
        ).read_text(encoding="utf-8")

        for token in ("nicegui", "excel_repository", "xlwings", "app.v13", "v15"):
            self.assertNotIn(token, source)
        self.assertIn("SegmentRepositoryPort", source)
        self.assertIn("PlanningCommandPort", source)
        self.assertIn("SegmentUpdateCommand", source)
        self.assertIn("ApplicationNotFoundError", source)
        self.assertNotIn("repository_context", source)
        self.assertNotIn("rebuild_planning", source)

if __name__ == "__main__":
    unittest.main()
