from __future__ import annotations

from dataclasses import replace
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


class _OperationalChoices:
    def __init__(self, events: list[object]) -> None:
        self.events = events

    def set_budget_override(self, demand_number, entry_key, hours, *, expected_version=None):
        self.events.append(("budget", demand_number, entry_key, hours))
        return object()


class _Targeting:
    def __init__(self, events: list[object]) -> None:
        self.events = events

    def assign_segment(self, segment_id: str, technician: str):
        self.events.append(("assign", segment_id, technician))
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

    def test_request_target_patch_uses_canonical_targeting_port(self) -> None:
        segments = _Segments()
        segments.record = replace(segments.record, origin="REQUEST")
        planning = _Planning(segments.events)
        targeting = _Targeting(segments.events)
        service = SegmentService(segments, planning, targeting=targeting)

        summary = service.update_command(
            SegmentUpdateCommand(segment_id="SEG-1", technician="Bob")
        )

        self.assertEqual(
            segments.events,
            [
                ("update", "SEG-1", {}),
                ("assign", "SEG-1", "Bob"),
            ],
        )
        self.assertEqual(summary["allocated_hours"], 8.0)

    def test_mixed_request_patch_syncs_effective_need_before_target_rebuild(self) -> None:
        segments = _Segments()
        segments.record = replace(
            segments.record,
            origin="REQUEST",
            planned_hours=40,
            approved_entry_key="ENTRY-1",
        )
        planning = _Planning(segments.events)
        targeting = _Targeting(segments.events)

        class Sync:
            def sync_operational_choices(self, number, **kwargs):
                segments.events.append(("sync", number, dict(kwargs)))

        service = SegmentService(
            segments,
            planning,
            approved_sync=Sync(),
            targeting=targeting,
            operational_choices=_OperationalChoices(segments.events),
        )

        summary = service.update_command(
            SegmentUpdateCommand(
                segment_id="SEG-1",
                technician="Bob",
                planned_hours=36,
            )
        )

        self.assertEqual(
            segments.events,
            [
                ("update", "SEG-1", {"HeuresPrevues": 36}),
                ("budget", "DMO-1", "ENTRY-1", 36.0),
                ("sync", "DMO-1", {}),
                ("assign", "SEG-1", "Bob"),
            ],
        )
        self.assertEqual(summary["allocated_hours"], 8.0)

    def test_request_target_clear_patch_is_rejected_before_mutation(self) -> None:
        segments = _Segments()
        segments.record = replace(segments.record, origin="REQUEST", resource_name="Alice")
        service = SegmentService(
            segments,
            _Planning(segments.events),
            targeting=_Targeting(segments.events),
        )

        with self.assertRaises(ApplicationValidationError) as raised:
            service.update_command(
                SegmentUpdateCommand(segment_id="SEG-1", technician=None)
            )

        self.assertEqual(raised.exception.code, "segment_target_clear_unsupported")
        self.assertEqual(segments.events, [])

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

    def test_update_passes_before_state_without_reusing_acknowledgment(self) -> None:
        segments = _Segments()
        segments.record = replace(
            segments.record,
            origin="REQUEST",
            planned_hours=20,
            approved_entry_key="ENTRY-1",
        )
        projection = {
            "current_planned_hours": 20,
            "current_excess_hours": 4,
            "excess_hours": 4,
        }
        segments.planned_hours_overallocation_context = lambda *_: dict(projection)

        class Sync:
            def __init__(self):
                self.calls = []

            def sync_operational_choices(self, number, **kwargs):
                self.calls.append((number, kwargs))

        sync = Sync()
        service = SegmentService(
            segments,
            _Planning(segments.events),
            approved_sync=sync,
            operational_choices=_OperationalChoices(segments.events),
        )
        service.update_command(SegmentUpdateCommand(
            segment_id="SEG-1", planned_hours=28, allow_locked_overallocation=True,
        ))
        service.update_command(SegmentUpdateCommand(segment_id="SEG-1", planned_hours=28))
        segments.record = replace(segments.record, planned_hours=24)
        projection.update(current_planned_hours=24, current_excess_hours=0, excess_hours=0)
        service.update_command(SegmentUpdateCommand(segment_id="SEG-1", planned_hours=32))

        self.assertEqual(sync.calls, [
            ("DMO-1", {
                "acknowledged_overallocation_segment_ids": ("SEG-1",),
                "pre_mutation_effective_hours_by_segment": {"SEG-1": 20.0},
            }),
            ("DMO-1", {"pre_mutation_effective_hours_by_segment": {"SEG-1": 20.0}}),
            ("DMO-1", {"pre_mutation_effective_hours_by_segment": {"SEG-1": 24.0}}),
        ])

    def test_quick_shift_choice_provides_safe_base_for_existing_adjustment_flow(self) -> None:
        segments = _Segments()
        segments.record = replace(
            segments.record, origin="QUICK_SHIFT", demand_number=None,
            planned_hours=12, locked_hours=10,
        )
        service = SegmentService(segments, _Planning(segments.events))
        with self.assertRaises(ApplicationValidationError) as raised:
            service.update_command(SegmentUpdateCommand(segment_id="SEG-1", planned_hours=8))
        self.assertEqual(raised.exception.code, "segment_overallocation_choice_required")
        safe_base = raised.exception.context["safe_operational_base_hours"]
        self.assertEqual(safe_base, 10)
        self.assertEqual(segments.events, [])
        service.update_command(SegmentUpdateCommand(segment_id="SEG-1", planned_hours=safe_base))
        self.assertEqual(segments.events[0], ("update", "SEG-1", {"HeuresPrevues": 10}))

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
