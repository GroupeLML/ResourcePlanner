from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from pathlib import Path
import unittest

from app.application.commands import DemandCreateCommand
from app.application.demand_service import DemandService
from app.application.errors import ApplicationValidationError


class _Demands:
    def __init__(self, create_record) -> None:
        self._create_record = create_record

    def list(self):
        return ()

    def get(self, _number):
        return None

    def create(self, values, *, submit=False):
        return self._create_record(values, submit)

    def update(self, *_args, **_kwargs):
        return None


class _Planning:
    def rebuild(self):
        return {}


class _Sync:
    def sync_approved(self, _number):
        return None


class DemandCreationServiceTests(unittest.TestCase):
    @staticmethod
    def _service(create_record, *, batch=None):
        return DemandService(
            _Demands(create_record),
            _Planning(),
            _Sync(),
            batch=batch,
        )

    def test_typed_create_forwards_business_payload_and_submit_intent(self) -> None:
        calls: list[object] = []

        def create_record(values: object, submit: bool) -> str:
            calls.append((values, submit))
            return "DMO-2026-0042"

        number = self._service(create_record).create_command(
            DemandCreateCommand(
                project_number="P-100",
                desired_start=date(2026, 8, 24),
                description="Travaux chantier",
                estimated_hours=8,
                submit=True,
            )
        )

        self.assertEqual(number, "DMO-2026-0042")
        self.assertEqual(calls[0][1], True)
        self.assertEqual(calls[0][0]["NumeroProjet"], "P-100")
        self.assertEqual(calls[0][0]["Description"], "Travaux chantier")
        self.assertEqual(calls[0][0]["DateDebutSouhaitee"], date(2026, 8, 24))

    def test_simple_create_forwards_canonical_resource_class(self) -> None:
        captured: dict[str, object] = {}

        def create_record(values: object, submit: bool) -> str:
            captured.update(values)
            return "DMO-2026-0535"

        self._service(create_record).create_command(
            DemandCreateCommand(
                project_number="P-535",
                desired_start=date(2026, 10, 1),
                required_resource_class="PROGRAMMEUR",
            )
        )

        self.assertEqual(captured["RequiredResourceClass"], "PROGRAMMEUR")

    def test_legacy_create_maps_canonical_resource_class(self) -> None:
        captured: dict[str, object] = {}

        def create_record(values: object, submit: bool) -> str:
            captured.update(values)
            return "DMO-2026-0536"

        self._service(create_record).create(
            {
                "NumeroProjet": "P-535",
                "DateDebutSouhaitee": "2026-10-01",
                "RequiredResourceClass": "PROGRAMMEUR",
            },
            submit=False,
        )

        self.assertEqual(captured["RequiredResourceClass"], "PROGRAMMEUR")

    def test_legacy_create_strips_workflow_owned_fields_via_closed_dto(self) -> None:
        captured: dict[str, object] = {}

        def create_record(values: object, submit: bool) -> str:
            captured.update(values)
            captured["_submit"] = submit
            return "DMO-2026-0043"

        self._service(create_record).create(
            {
                "NumeroProjet": "P-200",
                "DateDebutSouhaitee": "2026-08-25",
                "NoDemande": "FORGED",
                "Statut": "En planification",
                "ApprouvePar": "someone",
                "DateApprobation": "2026-08-01",
            },
            submit=False,
        )

        self.assertNotIn("NoDemande", captured)
        self.assertNotIn("Statut", captured)
        self.assertNotIn("ApprouvePar", captured)
        self.assertNotIn("DateApprobation", captured)
        self.assertEqual(captured["DateDebutSouhaitee"], date(2026, 8, 25))
        self.assertEqual(captured["_submit"], False)

    def test_create_requires_project_and_start_date_before_storage(self) -> None:
        writes: list[str] = []

        def create_record(_values: object, _submit: bool) -> str:
            writes.append("write")
            return "DMO-2026-0044"

        service = self._service(create_record)
        with self.assertRaises(ApplicationValidationError) as project_error:
            service.create({"DateDebutSouhaitee": "2026-08-24"})
        with self.assertRaises(ApplicationValidationError) as date_error:
            service.create({"NumeroProjet": "P-300"})

        self.assertEqual(project_error.exception.code, "demand_project_required")
        self.assertEqual(date_error.exception.code, "demand_start_required")
        self.assertEqual(writes, [])

    def test_create_runs_in_named_batch_when_available(self) -> None:
        events: list[object] = []

        @contextmanager
        def batch(label: str):
            events.append(("enter", label))
            try:
                yield
            finally:
                events.append(("exit", label))

        def create_record(_values: object, submit: bool) -> str:
            events.append(("create", submit))
            return "DMO-2026-0045"

        service = self._service(create_record, batch=batch)
        service.create(
            {"NumeroProjet": "P-400", "DateDebutSouhaitee": "2026-08-24"}
        )

        self.assertEqual(
            events,
            [("enter", "create demand"), ("create", False), ("exit", "create demand")],
        )




if __name__ == "__main__":
    unittest.main()
