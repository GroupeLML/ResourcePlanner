from __future__ import annotations

import ast
from datetime import date
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.application.commands import DemandUpdateCommand, SegmentCreateCommand
from app.application.demand_service import DemandService
from app.application.read_models import DemandReadModel, SegmentReadModel
from app.application.segment_service import SegmentService


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
    return modules


class _Planning:
    def __init__(self, result=None) -> None:
        self.result = result or {"engine": "pure"}

    def rebuild(self):
        return dict(self.result)


class _Sync:
    def __init__(self, events=None) -> None:
        self.events = events if events is not None else []

    def sync_approved(self, number):
        self.events.append(("sync", number))


class RepositoryPortTests(unittest.TestCase):
    def test_demand_read_model_normalizes_storage_row(self) -> None:
        model = DemandReadModel.from_mapping(
            {
                "NoDemande": " DMO-10 ",
                "Statut": "En planification",
                "NumeroProjet": 1234,
                "NomProjet": "Projet A",
                "DateDebutSouhaitee": "2026-08-25",
                "DateFinSouhaitee": date(2026, 8, 29),
            }
        )

        self.assertEqual(model.number, "DMO-10")
        self.assertEqual(model.project_number, "1234")
        self.assertEqual(model.desired_start, date(2026, 8, 25))
        self.assertEqual(model.desired_end, date(2026, 8, 29))

    def test_segment_read_model_normalizes_storage_row(self) -> None:
        model = SegmentReadModel.from_mapping(
            {
                "IDSegment": "SEG-1",
                "NoDemande": "DMO-1",
                "NumeroProjet": "P-1",
                "Technicien": "Alex",
                "DateDebut": "2026-08-25",
                "DateFin": "2026-08-26",
                "HeuresPrevues": "7,5",
                "Statut": "Planifié",
                "OrigineSegment": "QUICK_SHIFT",
            }
        )

        self.assertEqual(model.segment_id, "SEG-1")
        self.assertEqual(model.planned_hours, 7.5)
        self.assertEqual(model.start_date, date(2026, 8, 25))
        self.assertEqual(model.origin, "QUICK_SHIFT")

    def test_demand_service_composes_directly_against_ports_and_typed_commands(self) -> None:
        writes: list[tuple[object, ...]] = []
        sync_events: list[object] = []

        class Port:
            def __init__(self):
                self.status = "En planification"

            def list(self):
                return ()

            def get(self, number: str):
                return DemandReadModel(
                    number=number,
                    status=self.status,
                    desired_start=date(2026, 8, 25),
                    desired_end=date(2026, 8, 29),
                )

            def create(self, values, *, submit=False):
                writes.append(("create", dict(values), submit))
                return "DMO-NEW"

            def update(self, number, updates, *, action, comment=""):
                writes.append(("update", number, dict(updates), action, comment))
                if "Statut" in updates:
                    self.status = str(updates["Statut"])

        service = DemandService(
            Port(),
            _Planning(),
            _Sync(sync_events),
            current_user="coord-test-user",
        )

        self.assertTrue(
            service.modify_command(
                DemandUpdateCommand(number="DMO-1", description="révisée")
            )
        )
        service.approve("DMO-1", "ok")

        self.assertEqual(writes[0][0], "update")
        self.assertEqual(writes[0][2]["Statut"], "Soumise")
        approval = writes[1]
        self.assertEqual(approval[2]["Statut"], "En planification")
        self.assertEqual(approval[2]["ApprouvePar"], "coord-test-user")
        self.assertEqual(sync_events, [("sync", "DMO-1")])

    def test_segment_service_composes_directly_against_ports_and_typed_commands(self) -> None:
        writes: list[tuple[object, ...]] = []
        stored = SegmentReadModel(
            segment_id="SEG-NEW",
            demand_number="DMO-1",
            project_number="P-1",
            project_name="Projet",
            resource_name=None,
            start_date=date(2026, 8, 25),
            end_date=date(2026, 8, 25),
            planned_hours=8,
            status="À assigner",
        )

        class Port:
            def list(self, *, include_cancelled=True):
                return (stored,)

            def get(self, segment_id: str):
                return stored if segment_id == stored.segment_id else None

            def create(self, values):
                writes.append(("create", dict(values)))
                return "SEG-NEW"

            def update(self, segment_id, updates):
                writes.append(("update", segment_id, dict(updates)))

        service = SegmentService(Port(), _Planning())
        identifier, summary = service.create_command(
            SegmentCreateCommand(
                demand_number="DMO-1",
                start_date=date(2026, 8, 25),
                end_date=date(2026, 8, 25),
                planned_hours=8,
            )
        )
        service.cancel(identifier)

        self.assertEqual(identifier, "SEG-NEW")
        self.assertEqual(summary["engine"], "pure")
        self.assertEqual(writes[-1], ("update", "SEG-NEW", {"Statut": "Annulé"}))



    def test_application_repository_contracts_are_storage_neutral(self) -> None:
        forbidden_roots = {"xlwings", "sqlalchemy", "nicegui", "app.excel_repository"}
        for filename in (
            "application/read_models.py",
            "application/repository_ports.py",
            "application/command_ports.py",
            "application/demand_service.py",
            "application/segment_service.py",
        ):
            imports = imported_modules(APP / filename)
            offenders = {
                module
                for module in imports
                if any(
                    module == root or module.startswith(root + ".")
                    for root in forbidden_roots
                )
            }
            self.assertEqual(offenders, set(), f"{filename}: {offenders}")




if __name__ == "__main__":
    unittest.main()
