from __future__ import annotations

from datetime import date
from pathlib import Path
from types import MappingProxyType
import unittest

from app.domain.planning_engine import MISSING_ALLOCATION_TYPE, LockedAllocationInput, SegmentInput
from app.domain.planning_projection import (
    PlanningCalculationSnapshot,
    project_planning_snapshot,
)
from app.domain.planning_snapshot import PlanningSnapshot


D1 = date(2026, 8, 17)  # lundi
D2 = date(2026, 8, 18)
ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


class PlanningProjectionTests(unittest.TestCase):
    def _source_snapshot(self) -> PlanningSnapshot:
        return PlanningSnapshot.capture(
            segments=[
                {
                    "IDSegment": "S1",
                    "NoDemande": "D1",
                    "Technicien": "R1",
                    "DateDebut": D1,
                    "DateFin": D2,
                    "HeuresPrevues": 12,
                    "Statut": "Planifié",
                    "TypePlanification": "Flexible",
                    "DateCreation": "2026-08-01T08:00:00",
                    "HorsHoraireAutorise": "Oui",
                },
                {
                    "IDSegment": "S-BAD",
                    "NoDemande": "D1",
                    "Technicien": "R1",
                    "DateDebut": None,
                    "DateFin": None,
                    "HeuresPrevues": 4,
                    "Statut": "Planifié",
                },
                {
                    "IDSegment": "S-CANCELLED",
                    "NoDemande": "D1",
                    "Technicien": "R1",
                    "DateDebut": D1,
                    "DateFin": D1,
                    "HeuresPrevues": 4,
                    "Statut": "Annulé",
                },
                {
                    "IDSegment": "S-NO-SCHEDULE",
                    "NoDemande": "D1",
                    "Technicien": "R2",
                    "DateDebut": D1,
                    "DateFin": D1,
                    "HeuresPrevues": 4,
                    "Statut": "Planifié",
                },
            ],
            demands=[{"NoDemande": "D1", "Priorite": "Urgent"}],
            allocations=[
                {
                    "IDAllocation": "A-LOCK",
                    "IDSegment": "S1",
                    "Technicien": "R1",
                    "Date": D1,
                    "Heures": 2,
                    "Verrouillee": "Oui",
                    "HorsHoraire": "Non",
                    "TypeAllocation": "Flexible",
                },
                {
                    "IDAllocation": "A-AUTO",
                    "IDSegment": "S1",
                    "Technicien": "R1",
                    "Date": D1,
                    "Heures": 6,
                    "Verrouillee": "Non",
                    "HorsHoraire": "Non",
                    "TypeAllocation": "Flexible",
                },
                {
                    "IDAllocation": "A-MISSING",
                    "IDSegment": "S1",
                    "Technicien": "R1",
                    "Date": D2,
                    "Heures": 10,
                    "Verrouillee": "Non",
                    "HorsHoraire": "Non",
                    "TypeAllocation": MISSING_ALLOCATION_TYPE,
                },
                {
                    "IDAllocation": "A-LOCK-NO-SCHEDULE",
                    "IDSegment": "S-NO-SCHEDULE",
                    "Technicien": "R1",
                    "Date": D1,
                    "Heures": 3,
                    "Verrouillee": "Oui",
                    "HorsHoraire": "Non",
                    "TypeAllocation": "Flexible",
                },
                {
                    "IDAllocation": "A-LOCK-CANCELLED",
                    "IDSegment": "S-CANCELLED",
                    "Technicien": "R1",
                    "Date": D1,
                    "Heures": 4,
                    "Verrouillee": "Oui",
                    "HorsHoraire": "Non",
                    "TypeAllocation": "Flexible",
                },
                {
                    "IDAllocation": "A-IGNORED",
                    "IDSegment": "S-CANCELLED",
                    "Technicien": "R1",
                    "Date": D1,
                    "Heures": 4,
                    "Verrouillee": "Non",
                    "TypeAllocation": "Flexible",
                },
            ],
            availability=[
                {
                    "ID": "STD-R1",
                    "Type": "Horaire standard",
                    "Actif": "Oui",
                    "Technicien": "R1",
                    "JoursSemaine": "Lun,Mar,Mer,Jeu,Ven",
                    "HeureDebut": "08:00",
                    "HeureFin": "16:00",
                }
            ],
            technicians=[{"name": "R1"}, {"name": "R2"}],
        )

    def test_projection_normalizes_exact_engine_inputs(self) -> None:
        calculation = project_planning_snapshot(self._source_snapshot())

        self.assertIsInstance(calculation, PlanningCalculationSnapshot)
        self.assertEqual(
            calculation.segments,
            (
                SegmentInput(
                    segment_id="S1",
                    resource_id="R1",
                    start=D1,
                    end=D2,
                    hours=12.0,
                    plan_type="Flexible",
                    priority_rank=0,
                    created_order="2026-08-01T08:00:00",
                    overtime_allowed=True,
                ),
            ),
        )
        self.assertEqual(
            calculation.locked_allocations,
            (
                LockedAllocationInput(
                    segment_id="S1",
                    resource_id="R1",
                    day=D1,
                    hours=2.0,
                    outside_schedule=False,
                ),
                LockedAllocationInput(
                    segment_id="S-NO-SCHEDULE",
                    resource_id="R1",
                    day=D1,
                    hours=3.0,
                    outside_schedule=False,
                ),
            ),
        )
        self.assertEqual(
            calculation.preserved_segment_ids,
            frozenset({"S1", "S-BAD", "S-NO-SCHEDULE"}),
        )
        self.assertEqual(calculation.unsupported_segment_ids, ("S-BAD",))
        self.assertEqual(calculation.capacity_by_resource_day[("R1", D1)], 8.0)
        self.assertEqual(calculation.capacity_by_resource_day[("R1", D2)], 8.0)
        self.assertTrue(calculation.outside_schedule_eligible_by_resource_day[("R1", D1)])
        self.assertEqual(len(calculation.persisted_allocations), 3)
        self.assertEqual(
            [row.segment_id for row in calculation.persisted_allocations],
            ["S1", "S1", "S-NO-SCHEDULE"],
        )
        self.assertTrue(calculation.persisted_allocations[0].locked)
        self.assertEqual(calculation.persisted_allocations[0].allocation_type, "Locked")

    def test_capacity_and_eligibility_maps_are_read_only(self) -> None:
        calculation = project_planning_snapshot(self._source_snapshot())

        self.assertIsInstance(calculation.capacity_by_resource_day, MappingProxyType)
        self.assertIsInstance(
            calculation.outside_schedule_eligible_by_resource_day,
            MappingProxyType,
        )
        with self.assertRaises(TypeError):
            calculation.capacity_by_resource_day[("R1", D1)] = 99  # type: ignore[index]



    def test_projection_module_is_storage_and_ui_neutral(self) -> None:
        source = (APP / "domain" / "planning_projection.py").read_text(encoding="utf-8")
        lowered = source.lower()

        for forbidden in ("xlwings", "nicegui", "sqlalchemy", "excel_repository"):
            self.assertNotIn(forbidden, lowered)
        self.assertIn("PlanningCalculationSnapshot", source)
        self.assertIn("SegmentInput", source)
        self.assertIn("LockedAllocationInput", source)
        self.assertIn("AllocationProjection", source)


if __name__ == "__main__":
    unittest.main()
