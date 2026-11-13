from __future__ import annotations

from datetime import date
import unittest

from app.domain.operational_contacts import ContactResolution, STATUS_RESOLVED
from app.domain.project_communication import (
    ProjectCommunicationAssignment,
    ProjectCommunicationParticipant,
    build_project_communication_projection,
)


WEEK = date(2026, 9, 21)


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
