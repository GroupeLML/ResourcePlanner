from __future__ import annotations

from datetime import date
from dataclasses import replace
import unittest

from app.domain.operational_contacts import ContactResolution, STATUS_RESOLVED
from app.domain.project_communication import (
    ProjectCommunicationAsset,
    ProjectCommunicationAssignment,
    ProjectCommunicationParticipant,
    build_project_communication_projection,
    employee_assigned_assets,
)


WEEK = date(2026, 9, 21)


def synthetic_phone(last_four: str = "0100") -> str:
    return "514" + "-" + "555" + "-" + last_four


def participant(
    name: str,
    *,
    contact_id: str,
    user_id: str,
) -> ProjectCommunicationParticipant:
    return ProjectCommunicationParticipant(
        contact_id=contact_id,
        user_id=user_id,
        display_name=name,
        email=None,
        phone=None,
        active=True,
    )


def responsible(contact_id: str, name: str) -> ContactResolution:
    return ContactResolution(
        status=STATUS_RESOLVED,
        contact_id=contact_id,
        display_name=name,
        email=None,
        phone=None,
        source_type="TASK_RESPONSIBLE",
    )


class ProjectCommunicationProjectionTests(unittest.TestCase):
    def _row(
        self,
        *,
        shift_id: str,
        project_id: str = "P1",
        project_number: str = "1000",
        day: date = WEEK,
        hours: float = 4.0,
        confirmation: str = "Confirmée",
        outside_schedule: bool = False,
    ) -> ProjectCommunicationAssignment:
        return ProjectCommunicationAssignment(
            shift_id=shift_id,
            requirement_id="REQ1",
            project_id=project_id,
            project_number=project_number,
            project_name=f"Projet {project_number}",
            day=day,
            hours=hours,
            allocation_type="Flexible",
            outside_schedule=outside_schedule,
            confirmation=confirmation,
            task_id="TASK1",
            task_code="210",
            task_description="Installation de poteaux",
            resource_id="R1",
            resource_name="Alice",
            resource_contact=participant("Alice", contact_id="C-R1", user_id="U-R1"),
            project_manager=participant("Jean PM", contact_id="C-PM", user_id="U-PM"),
            operational_responsible=responsible("C-RESP", "Responsable chantier"),
        )

    def test_855_v2_preserves_shift_assets_standard_hours_and_operator_semantics(self) -> None:
        owned = ProjectCommunicationAsset(
            allocation_id="A-OWN",
            asset_id="ASSET-OWN",
            asset_code="MAT151",
            asset_label="Unité 151",
            asset_type_label="Véhicule",
            asset_category="VEHICLE",
            association_kind="OWNED_SHIFT",
        )
        related = ProjectCommunicationAsset(
            allocation_id="A-RELATED",
            asset_id="ASSET-RELATED",
            asset_code="MAT204",
            asset_label="Unité 204",
            asset_type_label="Véhicule",
            asset_category="VEHICLE",
            association_kind="RELATED_REQUEST",
        )
        rows = (
            replace(
                self._row(shift_id="S1", hours=5.0),
                assets=(owned, related),
                standard_hours=7.5,
            ),
            replace(
                self._row(shift_id="S2", hours=3.0),
                assets=(),
                standard_hours=7.5,
            ),
        )

        result = build_project_communication_projection(
            week_start=WEEK,
            week_end=date(2026, 9, 27),
            assignments=rows,
            request_provenance_version=2,
        )

        self.assertEqual(result.request_provenance_version, 2)
        assignments = result.projects[0].days[0].tasks[0].request_assignments
        self.assertEqual(assignments[0].assets, (owned, related))
        self.assertEqual(assignments[0].standard_hours, 7.5)
        self.assertEqual(assignments[1].assets, ())
        self.assertEqual(
            [asset.asset_code for asset in employee_assigned_assets(assignments[0])],
            ["MAT151"],
        )
        self.assertEqual(
            [asset.asset_code for asset in result.projects[0].days[0].tasks[0].resources[0].assets],
            ["MAT151"],
        )

    def test_855_v2_rejects_missing_standard_hours(self) -> None:
        with self.assertRaisesRegex(ValueError, "Heures standard"):
            build_project_communication_projection(
                week_start=WEEK,
                week_end=date(2026, 9, 27),
                assignments=(self._row(shift_id="S1"),),
                request_provenance_version=2,
            )

    def test_831_manager_erp_phone_propagates_without_changing_shift_grouping(self) -> None:
        rows = (
            replace(self._row(shift_id="S1", hours=3.0),
                    project_manager_erp_phone=synthetic_phone("0100")),
            replace(self._row(shift_id="S2", hours=5.0),
                    project_manager_erp_phone=synthetic_phone("0100")),
        )
        result = build_project_communication_projection(
            week_start=WEEK, week_end=date(2026, 9, 27), assignments=rows
        )
        self.assertEqual(result.projects[0].project_manager_erp_phone, synthetic_phone("0100"))
        task = result.projects[0].days[0].tasks[0]
        self.assertEqual(task.resources[0].hours, 8.0)
        self.assertEqual(task.resources[0].shift_ids, ("S1", "S2"))
        self.assertEqual(len(task.request_assignments), 2)

    def test_groups_project_day_task_and_deduplicates_resource(self) -> None:
        result = build_project_communication_projection(
            week_start=WEEK,
            week_end=date(2026, 9, 27),
            assignments=(
                self._row(shift_id="S1", hours=3.0),
                self._row(
                    shift_id="S2",
                    hours=5.0,
                    confirmation="Tentative",
                    outside_schedule=True,
                ),
            ),
        )

        self.assertEqual(len(result.projects), 1)
        task = result.projects[0].days[0].tasks[0]
        self.assertEqual(task.task_description, "Installation de poteaux")
        self.assertEqual(len(task.resources), 1)
        resource = task.resources[0]
        self.assertEqual(resource.hours, 8.0)
        self.assertEqual(resource.shift_ids, ("S1", "S2"))
        self.assertEqual(resource.confirmations, ("Confirmée", "Tentative"))
        self.assertTrue(resource.outside_schedule)

    def test_830_shift_identity_is_unique_and_provenance_is_not_double_counted(self) -> None:
        result = build_project_communication_projection(
            week_start=WEEK, week_end=date(2026, 9, 27),
            assignments=(self._row(shift_id="S1", hours=3), self._row(shift_id="S2", hours=5)),
        )
        task = result.projects[0].days[0].tasks[0]
        self.assertEqual(task.resources[0].hours, 8)
        self.assertEqual({row.shift_id for row in task.request_assignments}, {"S1", "S2"})
        self.assertTrue(result.request_provenance_complete)
        with self.assertRaises(ValueError):
            build_project_communication_projection(
                week_start=WEEK, week_end=date(2026, 9, 27),
                assignments=(self._row(shift_id="S1"), self._row(shift_id="S1")),
            )

    def test_same_manager_can_still_produce_two_project_groups(self) -> None:
        result = build_project_communication_projection(
            week_start=WEEK,
            week_end=date(2026, 9, 27),
            assignments=(
                self._row(shift_id="S1"),
                self._row(
                    shift_id="S2",
                    project_id="P2",
                    project_number="2000",
                ),
            ),
        )

        self.assertEqual(
            [row.project_number for row in result.projects],
            ["1000", "2000"],
        )


if __name__ == "__main__":
    unittest.main()
