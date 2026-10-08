from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

from app.application.commands import (
    DemandApproveCommand,
    DemandLineInput,
    DemandUpdateCommand,
)
from app.application.demand_service import DemandService
from app.application.errors import (
    ApplicationConflictError,
    ApplicationNotFoundError,
    ApplicationOperationError,
    ApplicationValidationError,
)
from app.application.read_models import DemandPeriodReadModel, DemandReadModel
from app.domain.demand_periods import DemandPeriodDefinition


class _Demands:
    def __init__(self, record: DemandReadModel | None = None, events=None) -> None:
        self.record = record
        self.events = events if events is not None else []
        self.next_number = "DMO-NEW"

    def list(self):
        return (self.record,) if self.record is not None else ()

    def get(self, number):
        if self.record is not None and self.record.number == number:
            return self.record
        return None

    def create(self, values, *, submit=False):
        self.events.append(("create", dict(values), submit))
        return self.next_number

    def update(self, number, updates, *, action, comment=""):
        self.events.append(("update", number, dict(updates), action, comment))


class _Planning:
    def __init__(self, events, failure: Exception | None = None) -> None:
        self.events = events
        self.failure = failure

    def rebuild(self):
        self.events.append(("rebuild",))
        if self.failure is not None:
            raise self.failure
        return {"allocated_hours": 40.0, "engine": "pure"}


class _Sync:
    def __init__(self, events, failure: Exception | None = None) -> None:
        self.events = events
        self.failure = failure

    def sync_approved(self, number):
        self.events.append(("sync", number))
        if self.failure is not None:
            raise self.failure


class DemandServiceTests(unittest.TestCase):
    def test_simple_resource_class_update_preserves_unset_clear_and_replace_contract(self) -> None:
        omitted = DemandUpdateCommand(number="DMO-535").to_repository_values()
        cleared = DemandUpdateCommand(
            number="DMO-535",
            required_resource_class=None,
        ).to_repository_values()
        replaced = DemandUpdateCommand(
            number="DMO-535",
            required_resource_class="INSTALLATEUR_AUTOMATISATION",
        ).to_repository_values()

        self.assertNotIn("RequiredResourceClass", omitted)
        self.assertIn("RequiredResourceClass", cleared)
        self.assertIsNone(cleared["RequiredResourceClass"])
        self.assertEqual(
            replaced["RequiredResourceClass"],
            "INSTALLATEUR_AUTOMATISATION",
        )


    @staticmethod
    def _service(
        *,
        record: DemandReadModel | None = None,
        events=None,
        current_user="coordinator",
        batch=None,
        sync_failure=None,
        planning_failure=None,
    ):
        events = events if events is not None else []
        demands = _Demands(record, events)
        return (
            DemandService(
                demands,
                _Planning(events, planning_failure),
                _Sync(events, sync_failure),
                current_user=current_user,
                batch=batch,
            ),
            demands,
            events,
        )

    def test_create_strips_workflow_fields_and_validates_required_data(self) -> None:
        service, _demands, events = self._service()

        number = service.create(
            {
                "NumeroProjet": "5094",
                "DateDebutSouhaitee": "2026-08-25",
                "Description": "Travail",
                "TempsEstimeHeures": 8,
                "Statut": "En planification",
                "NoDemande": "FORBIDDEN",
                "ApprouvePar": "x",
            },
            submit=True,
        )

        self.assertEqual(number, "DMO-NEW")
        payload = events[0][1]
        self.assertNotIn("Statut", payload)
        self.assertNotIn("NoDemande", payload)
        self.assertNotIn("ApprouvePar", payload)
        self.assertEqual(payload["DateDebutSouhaitee"], date(2026, 8, 25))
        self.assertTrue(events[0][2])

        with self.assertRaises(ApplicationValidationError):
            service.create({"NumeroProjet": "", "DateDebutSouhaitee": "2026-08-25"})
        with self.assertRaises(ApplicationValidationError):
            service.create({"NumeroProjet": "5094"})

    def test_modify_approved_business_demand_requires_reapproval_without_touching_plan(self) -> None:
        events: list[object] = []

        @contextmanager
        def batch(label: str):
            events.append(("batch-enter", label))
            try:
                yield
            finally:
                events.append(("batch-exit", label))

        service, _demands, _ = self._service(
            record=DemandReadModel(
                number="DMO-EDIT",
                status="En planification",
                desired_start=date(2026, 8, 25),
                desired_end=date(2026, 8, 29),
            ),
            events=events,
            batch=batch,
        )
        required = service.modify_command(
            DemandUpdateCommand.from_mapping(
                "DMO-EDIT",
                {"Description": "Nouvelle portée", "Confirmation": "Tentative"},
                comment="Demande modifiée dans l'application",
            )
        )

        self.assertTrue(required)
        self.assertEqual(events[0], ("batch-enter", "modify demand"))
        self.assertEqual(events[-1], ("batch-exit", "modify demand"))
        write = events[1]
        self.assertEqual(write[0], "update")
        updates = write[2]
        self.assertEqual(updates["Statut"], "Soumise")
        self.assertIsNone(updates["ApprouvePar"])
        self.assertIsNone(updates["DateApprobation"])
        self.assertIn("nouvelle approbation requise", updates["CommentaireApprobation"])
        self.assertIn("planification existante est conservée", write[4])
        self.assertFalse(
            any(
                event[0] in {"sync", "rebuild"}
                for event in events
                if isinstance(event, tuple)
            )
        )

    def test_approved_demand_line_edit_requests_reapproval_without_touching_plan(self) -> None:
        service, _demands, events = self._service(
            record=DemandReadModel(
                number="DMO-APPROVED",
                status="En planification",
                desired_start=date(2026, 8, 25),
                desired_end=date(2026, 8, 29),
                version=1,
                line_mode=True,
            )
        )

        required = service.modify_command(
            DemandUpdateCommand(
                number="DMO-APPROVED",
                expected_version=1,
                lines=(
                    DemandLineInput(
                        line_id="LINE-1",
                        desired_start=date(2026, 8, 25),
                        desired_active_days=1,
                        estimated_hours=8,
                    ),
                ),
            )
        )

        self.assertTrue(required)
        self.assertEqual(events[0][0], "update")
        self.assertEqual(events[0][2]["Statut"], "Soumise")
        self.assertEqual(events[0][2]["ExpectedVersion"], 1)
        self.assertEqual(events[0][2]["RequestLines"][0]["id"], "LINE-1")
        self.assertFalse(any(event[0] in {"sync", "rebuild"} for event in events))

    def test_period_change_detection_includes_desired_active_days(self) -> None:
        definition = DemandPeriodDefinition(
            period_id="PER-1",
            start_date=date(2026, 8, 25),
            end_date=date(2026, 8, 29),
            hours=24,
            desired_active_days=3,
        )
        persisted = DemandPeriodReadModel(
            period_id="PER-1",
            demand_number="DMO-PERIOD",
            sequence=0,
            kind="CUMULATIVE",
            start_date=date(2026, 8, 25),
            end_date=date(2026, 8, 29),
            hours=24,
            confirmation="Tentative",
            desired_active_days=2,
        )

        self.assertNotEqual(
            DemandService._period_signature_from_definition(definition),
            DemandService._period_signature_from_read_model(persisted),
        )

    def test_modify_unapproved_demand_does_not_force_reapproval(self) -> None:
        service, _demands, events = self._service(
            record=DemandReadModel(
                number="DMO-DRAFT",
                status="Brouillon",
                desired_start=date(2026, 8, 25),
                desired_end=date(2026, 8, 29),
            )
        )

        required = service.modify(
            "DMO-DRAFT",
            {"Description": "Brouillon modifié"},
            "Modification",
        )

        self.assertFalse(required)
        self.assertEqual(events[0][2], {"Description": "Brouillon modifié"})

    def test_modify_unknown_demand_is_structured_not_found(self) -> None:
        service, _demands, events = self._service(record=None)
        with self.assertRaises(ApplicationNotFoundError) as raised:
            service.modify("DMO-MISSING", {"Description": "x"})
        self.assertEqual(raised.exception.code, "demand_not_found")
        self.assertEqual(raised.exception.context["demand_number"], "DMO-MISSING")
        self.assertEqual(events, [])

    def test_modify_command_validates_date_window_against_existing_read_model(self) -> None:
        service, _demands, events = self._service(
            record=DemandReadModel(
                number="DMO-DATES",
                status="Brouillon",
                desired_start=date(2026, 8, 25),
                desired_end=date(2026, 8, 30),
            )
        )

        with self.assertRaises(ApplicationValidationError) as raised:
            service.modify_command(
                DemandUpdateCommand(
                    number="DMO-DATES",
                    desired_start=date(2026, 9, 1),
                )
            )

        self.assertEqual(raised.exception.code, "demand_date_window_invalid")
        self.assertEqual(events, [])

    def test_typed_approve_orders_record_sync_rebuild_inside_one_batch(self) -> None:
        events: list[object] = []

        @contextmanager
        def batch(label: str):
            events.append(("batch-enter", label))
            try:
                yield
            finally:
                events.append(("batch-exit", label))

        service, _demands, _ = self._service(
            record=DemandReadModel(number="DMO-1", status="Soumise"),
            events=events,
            batch=batch,
        )
        result = service.approve_command(DemandApproveCommand("DMO-1", "ok"))

        self.assertEqual(
            [event[0] for event in events],
            ["batch-enter", "update", "sync", "rebuild", "batch-exit"],
        )
        update = events[1]
        self.assertEqual(update[2]["Statut"], "En planification")
        self.assertEqual(update[2]["ApprouvePar"], "coordinator")
        self.assertEqual(update[3], "Approbation")
        self.assertEqual(result["allocated_hours"], 40.0)

    def test_sync_failure_stops_rebuild_and_is_structured(self) -> None:
        events: list[object] = []
        service, _demands, _ = self._service(
            record=DemandReadModel(number="DMO-2", status="Soumise"),
            events=events,
            sync_failure=RuntimeError("sync failed"),
        )

        with self.assertRaises(ApplicationOperationError) as raised:
            service.approve("DMO-2")

        self.assertEqual(raised.exception.code, "demand_approval_sync_failed")
        self.assertEqual(str(raised.exception), "sync failed")
        self.assertEqual([event[0] for event in events], ["update", "sync"])

    def test_submit_resolves_legacy_days_to_eight_hour_workdays(self) -> None:
        service, _demands, events = self._service(
            record=DemandReadModel(
                number="DMO-DAYS",
                status="Brouillon",
                desired_start=date(2026, 8, 24),
                desired_end=date(2026, 8, 28),
                resource_count=2,
                estimated_days=3,
                estimated_hours=None,
            )
        )

        service.submit("DMO-DAYS")

        self.assertEqual(events[0][0], "update")
        self.assertEqual(events[0][2]["Statut"], "Soumise")
        self.assertEqual(events[0][2]["TempsEstimeHeures"], 48.0)
        self.assertEqual(events[0][2]["RequestLineHoursSource"], "DEFAULT_8H")

    def test_simple_lifecycle_transitions_preserve_status_and_audit_semantics(self) -> None:
        service, demands, events = self._service(
            record=DemandReadModel(
                number="DMO-10",
                status="Brouillon",
                desired_start=date(2026, 8, 25),
                estimated_hours=8,
            )
        )
        service.submit("DMO-10")
        demands.record = DemandReadModel(number="DMO-11", status="Soumise")
        service.request_correction("DMO-11", "  préciser la date  ")
        demands.record = DemandReadModel(number="DMO-12", status="En planification")
        service.cancel("DMO-12")

        self.assertEqual(events[0][2], {"Statut": "Soumise"})
        self.assertEqual(events[0][3], "Soumission")
        self.assertEqual(events[1][2]["Statut"], "À corriger")
        self.assertEqual(events[1][2]["CommentaireApprobation"], "préciser la date")
        self.assertEqual(events[1][3], "Retour pour correction")
        self.assertEqual(events[2][2], {"Statut": "Annulée"})
        self.assertEqual(events[2][3], "Annulation")

    def test_correction_reason_is_structured_validation_error(self) -> None:
        service, _demands, events = self._service()
        with self.assertRaises(ApplicationValidationError) as raised:
            service.request_correction("DMO-20", "   ")
        self.assertEqual(raised.exception.code, "demand_correction_comment_required")
        self.assertEqual(events, [])


    def test_application_service_has_only_port_and_command_dependencies(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "application"
            / "demand_service.py"
        ).read_text(encoding="utf-8")
        for token in (
            "nicegui",
            "xlwings",
            "excel_repository",
            "v15_engine",
            "v15_refinements",
            "repository_context",
            "sync_approved_demand: Callable",
            "rebuild_planning: Callable",
        ):
            self.assertNotIn(token, source)
        self.assertIn("DemandRepositoryPort", source)
        self.assertIn("PlanningCommandPort", source)
        self.assertIn("ApprovedDemandSyncPort", source)
        self.assertIn("DemandUpdateCommand", source)
        self.assertIn("call_application_port", source)
        self.assertIn("ApplicationValidationError", source)


if __name__ == "__main__":
    unittest.main()
