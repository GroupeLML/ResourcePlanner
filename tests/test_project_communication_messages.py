from __future__ import annotations

from datetime import date
import unittest

from app.domain.operational_contacts import ContactResolution, STATUS_RESOLVED
from app.domain.project_communication import (
    ProjectCommunicationDay,
    ProjectCommunicationParticipant,
    ProjectCommunicationProject,
    ProjectCommunicationProjection,
    ProjectCommunicationResource,
    ProjectCommunicationResponsibilityAssignment,
    ProjectCommunicationTask,
)
from app.domain.project_communication_messages import (
    DIAGNOSTIC_CC_EMAIL_MISSING,
    DIAGNOSTIC_CO_MANAGER_TO_EMAIL_MISSING,
    DIAGNOSTIC_TO_EMAIL_MISSING,
    MESSAGE_KIND_PLANNING_CHANGE,
    MESSAGE_KIND_WEEKLY_CONFIRMATION,
    SEVERITY_BLOCKING,
    SEVERITY_WARNING,
    build_project_confirmation_batch,
    build_project_delta_batch,
    compare_project_communication_projections,
    deserialize_project_projection,
    french_long_date,
    project_projection_fingerprint,
    serialize_project_projection,
)


WEEK = date(2026, 9, 21)


def email(local: str) -> str:
    return local + chr(64) + "invalid.test"


def participant(
    name: str,
    *,
    contact_id: str,
    user_id: str,
    address: str | None,
    active: bool = True,
) -> ProjectCommunicationParticipant:
    return ProjectCommunicationParticipant(
        contact_id=contact_id,
        user_id=user_id,
        display_name=name,
        email=address,
        phone=None,
        active=active,
    )


def responsible(
    name: str = "Jean Responsable",
    *,
    contact_id: str = "C-RESP",
    phone: str | None = None,
) -> ContactResolution:
    return ContactResolution(
        status=STATUS_RESOLVED,
        contact_id=contact_id,
        display_name=name,
        email=None,
        phone=phone,
        source_type="TASK_RESPONSIBLE",
        source_entity_id="TASK-1",
    )


def resource(
    resource_id: str,
    name: str,
    *,
    address: str | None,
    hours: float = 8.0,
    confirmation: str = "Confirmée",
    outside: bool = False,
) -> ProjectCommunicationResource:
    return ProjectCommunicationResource(
        resource_id=resource_id,
        resource_name=name,
        contact=participant(
            name,
            contact_id=f"C-{resource_id}",
            user_id=f"U-{resource_id}",
            address=address,
        ),
        hours=hours,
        shift_ids=(f"S-{resource_id}",),
        allocation_types=("Flexible",),
        confirmations=(confirmation,),
        outside_schedule=outside,
    )


def project(
    project_id: str = "P1",
    number: str = "1000",
    *,
    manager_address: str | None = None,
    manager_contact_id: str = "C-PM",
    manager_user_id: str = "U-PM",
    resources: tuple[ProjectCommunicationResource, ...] | None = None,
    second_day: bool = False,
    task_description: str = "Installation de poteaux",
    responsible_contact: ContactResolution | None = None,
    co_managers: tuple[ProjectCommunicationParticipant, ...] = (),
) -> ProjectCommunicationProject:
    pm = participant(
        "Chargé Démo",
        contact_id=manager_contact_id,
        user_id=manager_user_id,
        address=manager_address if manager_address is not None else email("pm"),
    )
    rows = resources or (
        resource("R1", "Alice", address=email("alice")),
        resource("R2", "Bob", address=email("bob")),
    )
    resp = responsible_contact or responsible(phone="555" + "-" + "0100")
    tasks = (
        ProjectCommunicationTask(
            task_description=task_description,
            task_ids=("TASK-1",),
            task_codes=("210",),
            operational_responsibles=(resp,),
            resources=rows,
        ),
    )
    days = [ProjectCommunicationDay(day=WEEK, tasks=tasks)]
    if second_day:
        days.append(
            ProjectCommunicationDay(
                day=date(2026, 9, 22),
                tasks=(
                    ProjectCommunicationTask(
                        task_description="Mise en service",
                        task_ids=("TASK-2",),
                        task_codes=("220",),
                        operational_responsibles=(resp,),
                        resources=(rows[0],),
                    ),
                ),
            )
        )
    return ProjectCommunicationProject(
        project_id=project_id,
        project_number=number,
        project_name=f"Projet {number}",
        project_manager=pm,
        days=tuple(days),
        project_co_managers=co_managers,
    )


def projection(*projects: ProjectCommunicationProject) -> ProjectCommunicationProjection:
    return ProjectCommunicationProjection(
        week_start=WEEK,
        week_end=date(2026, 9, 27),
        projects=projects,
    )


class ProjectCommunicationMessageTests(unittest.TestCase):
    def test_french_long_date_is_derived_from_real_date(self) -> None:
        self.assertEqual(
            french_long_date(date(2026, 9, 22)),
            "Mardi le 22 septembre 2026",
        )

    def test_one_project_with_multiple_resources_creates_one_project_draft(self) -> None:
        batch = build_project_confirmation_batch(projection(project()))

        self.assertEqual(len(batch.drafts), 1)
        draft = batch.drafts[0]
        self.assertEqual(draft.message_key, "project:P1")
        self.assertEqual(draft.audience, "project")
        self.assertEqual(draft.message_kind, MESSAGE_KIND_WEEKLY_CONFIRMATION)
        self.assertEqual(draft.to_recipient.email, email("pm"))
        self.assertEqual(
            [row.email for row in draft.to_recipients],
            [email("pm")],
        )
        self.assertEqual(
            {row.email for row in draft.cc_recipients},
            {email("alice"), email("bob")},
        )
        self.assertTrue(draft.approvable)

    def test_co_managers_are_added_to_to_and_excluded_from_resource_cc(self) -> None:
        co_a = participant(
            "Co chargé A",
            contact_id="C-CO-A",
            user_id="U-CO-A",
            address=email("co-a"),
        )
        co_b = participant(
            "Co chargé B",
            contact_id="C-CO-B",
            user_id="U-CO-B",
            address=email("co-b"),
        )
        rows = (
            resource("R1", "Co chargé aussi ressource", address=email("co-a")),
            resource("R2", "Bob", address=email("bob")),
        )

        draft = build_project_confirmation_batch(
            projection(
                project(
                    resources=rows,
                    co_managers=(co_a, co_b),
                )
            )
        ).drafts[0]

        self.assertEqual(
            [row.email for row in draft.to_recipients],
            [email("pm"), email("co-a"), email("co-b")],
        )
        self.assertEqual(
            [row.email for row in draft.cc_recipients],
            [email("bob")],
        )
        self.assertTrue(draft.approvable)

    def test_to_recipients_are_deduplicated_by_explicit_address(self) -> None:
        duplicate = participant(
            "Autre identité",
            contact_id="C-CO-DUP",
            user_id="U-CO-DUP",
            address=email("pm"),
        )

        draft = build_project_confirmation_batch(
            projection(project(co_managers=(duplicate,)))
        ).drafts[0]

        self.assertEqual(
            [row.email for row in draft.to_recipients],
            [email("pm")],
        )

    def test_missing_co_manager_email_is_explicit_blocking_diagnostic(self) -> None:
        missing = participant(
            "Co chargé sans courriel",
            contact_id="C-CO-NO-MAIL",
            user_id="U-CO-NO-MAIL",
            address=None,
        )

        draft = build_project_confirmation_batch(
            projection(project(co_managers=(missing,)))
        ).drafts[0]

        diagnostic = next(
            row
            for row in draft.diagnostics
            if row.code == DIAGNOSTIC_CO_MANAGER_TO_EMAIL_MISSING
        )
        self.assertEqual(diagnostic.entity_type, "project_co_manager")
        self.assertEqual(diagnostic.entity_id, "C-CO-NO-MAIL")
        self.assertEqual(diagnostic.severity, SEVERITY_BLOCKING)
        self.assertFalse(draft.approvable)

        restored = deserialize_project_projection(
            serialize_project_projection(
                projection(project(co_managers=(missing,)))
            )
        )
        self.assertEqual(
            restored.projects[0].project_co_managers[0].contact_id,
            "C-CO-NO-MAIL",
        )

    def test_cc_is_deduplicated_by_explicit_email_and_excludes_to(self) -> None:
        shared = email("shared")
        rows = (
            resource("R1", "Alice", address=shared),
            resource("R2", "Alice secondaire", address=shared),
            resource("R3", "Chargé aussi ressource", address=email("pm")),
        )
        draft = build_project_confirmation_batch(
            projection(project(resources=rows))
        ).drafts[0]

        self.assertEqual([row.email for row in draft.cc_recipients], [shared])

    def test_same_manager_two_projects_still_creates_two_isolated_drafts(self) -> None:
        batch = build_project_confirmation_batch(
            projection(
                project(project_id="P1", number="1000"),
                project(project_id="P2", number="2000"),
            )
        )

        self.assertEqual(
            [draft.message_key for draft in batch.drafts],
            ["project:P1", "project:P2"],
        )
        self.assertTrue(all(draft.to_recipient.email == email("pm") for draft in batch.drafts))

    def test_resource_without_email_is_visible_warning_but_not_fabricated(self) -> None:
        missing = resource("R-NO-MAIL", "Sans courriel", address=None)
        draft = build_project_confirmation_batch(
            projection(project(resources=(missing,)))
        ).drafts[0]

        self.assertEqual(draft.cc_recipients, ())
        diagnostic = next(
            row for row in draft.diagnostics if row.code == DIAGNOSTIC_CC_EMAIL_MISSING
        )
        self.assertEqual(diagnostic.entity_id, "R-NO-MAIL")
        self.assertEqual(diagnostic.severity, SEVERITY_WARNING)
        self.assertTrue(draft.approvable)

    def test_missing_project_manager_email_blocks_approval(self) -> None:
        draft = build_project_confirmation_batch(
            projection(project(manager_address=""))
        ).drafts[0]

        diagnostic = next(
            row for row in draft.diagnostics if row.code == DIAGNOSTIC_TO_EMAIL_MISSING
        )
        self.assertEqual(diagnostic.severity, SEVERITY_BLOCKING)
        self.assertFalse(draft.approvable)

    def test_body_groups_multiple_days_tasks_and_includes_responsible_phone(self) -> None:
        draft = build_project_confirmation_batch(
            projection(project(second_day=True))
        ).drafts[0]

        self.assertIn("1000 — Projet 1000", draft.body)
        self.assertIn("Responsable : Jean Responsable — " + "555" + "-" + "0100", draft.body)
        self.assertIn("Lundi le 21 septembre 2026 — Installation de poteaux", draft.body)
        self.assertIn("Mardi le 22 septembre 2026 — Mise en service", draft.body)
        self.assertIn("  • Alice — 8 h", draft.body)
        self.assertNotIn("U-R1", draft.body)
        self.assertNotIn("C-R1", draft.body)

    def test_multiple_responsibles_remain_associated_with_each_assignment(self) -> None:
        alice = resource("R1", "Alice", address=email("alice"))
        bob = resource("R2", "Bob", address=email("bob"))
        first = responsible("Responsable Alice", contact_id="C-A")
        second = responsible("Responsable Bob", contact_id="C-B")
        task = ProjectCommunicationTask(
            task_description="Installation de poteaux",
            task_ids=("TASK-1",),
            task_codes=("210",),
            operational_responsibles=(first, second),
            resources=(alice, bob),
            responsibility_assignments=(
                ProjectCommunicationResponsibilityAssignment(
                    shift_id="S-R1",
                    resource_id="R1",
                    resource_name="Alice",
                    operational_responsible=first,
                ),
                ProjectCommunicationResponsibilityAssignment(
                    shift_id="S-R2",
                    resource_id="R2",
                    resource_name="Bob",
                    operational_responsible=second,
                ),
            ),
        )
        source = project(resources=(alice, bob))
        source = ProjectCommunicationProject(
            project_id=source.project_id,
            project_number=source.project_number,
            project_name=source.project_name,
            project_manager=source.project_manager,
            days=(ProjectCommunicationDay(day=WEEK, tasks=(task,)),),
        )

        draft = build_project_confirmation_batch(projection(source)).drafts[0]

        self.assertIn(
            "Alice — 8 h · Responsable : Responsable Alice",
            draft.body,
        )
        self.assertIn(
            "Bob — 8 h · Responsable : Responsable Bob",
            draft.body,
        )
        self.assertNotIn("Responsable Alice / Responsable Bob", draft.body)
        self.assertEqual(
            {row.email for row in draft.cc_recipients},
            {email("alice"), email("bob")},
        )

        restored = deserialize_project_projection(
            serialize_project_projection(projection(source))
        )
        assignments = restored.projects[0].days[0].tasks[0].responsibility_assignments
        self.assertEqual(
            {
                (row.shift_id, row.resource_id, row.operational_responsible.contact_id)
                for row in assignments
            },
            {("S-R1", "R1", "C-A"), ("S-R2", "R2", "C-B")},
        )

    def test_responsibility_reassignment_changes_fingerprint_without_recipient_change(self) -> None:
        alice = resource("R1", "Alice", address=email("alice"))
        bob = resource("R2", "Bob", address=email("bob"))
        first = responsible("Responsable Alice", contact_id="C-A")
        second = responsible("Responsable Bob", contact_id="C-B")

        def with_assignments(
            pairs: tuple[tuple[ProjectCommunicationResource, ContactResolution], ...],
        ) -> ProjectCommunicationProject:
            base = project(resources=(alice, bob))
            task = ProjectCommunicationTask(
                task_description="Installation de poteaux",
                task_ids=("TASK-1",),
                task_codes=("210",),
                operational_responsibles=(first, second),
                resources=(alice, bob),
                responsibility_assignments=tuple(
                    ProjectCommunicationResponsibilityAssignment(
                        shift_id=row.shift_ids[0],
                        resource_id=row.resource_id,
                        resource_name=row.resource_name,
                        operational_responsible=contact,
                    )
                    for row, contact in pairs
                ),
            )
            return ProjectCommunicationProject(
                project_id=base.project_id,
                project_number=base.project_number,
                project_name=base.project_name,
                project_manager=base.project_manager,
                days=(ProjectCommunicationDay(day=WEEK, tasks=(task,)),),
            )

        before = projection(with_assignments(((alice, first), (bob, second))))
        after = projection(with_assignments(((alice, second), (bob, first))))
        batch = build_project_delta_batch(before, after)

        self.assertEqual(len(batch.drafts), 1)
        self.assertEqual(
            {row.email for row in batch.drafts[0].cc_recipients},
            {email("alice"), email("bob")},
        )
        self.assertNotEqual(
            project_projection_fingerprint(before),
            project_projection_fingerprint(after),
        )

    def test_tentative_and_outside_schedule_are_preserved_in_body(self) -> None:
        row = resource(
            "R1",
            "Alice",
            address=email("alice"),
            confirmation="Tentative",
            outside=True,
        )
        draft = build_project_confirmation_batch(
            projection(project(resources=(row,)))
        ).drafts[0]

        self.assertIn("Alice — 8 h (Tentative, hors horaire)", draft.body)

    def test_fingerprint_changes_when_hours_change(self) -> None:
        first = build_project_confirmation_batch(
            projection(
                project(
                    resources=(
                        resource("R1", "Alice", address=email("alice"), hours=4),
                    )
                )
            )
        )
        second = build_project_confirmation_batch(
            projection(
                project(
                    resources=(
                        resource("R1", "Alice", address=email("alice"), hours=8),
                    )
                )
            )
        )

        self.assertNotEqual(first.drafts[0].body, second.drafts[0].body)
        self.assertNotEqual(
            first.drafts[0].content_fingerprint,
            second.drafts[0].content_fingerprint,
        )
        self.assertNotEqual(first.snapshot_fingerprint, second.snapshot_fingerprint)

    def test_projection_snapshot_round_trip_preserves_delta_state(self) -> None:
        original = projection(project(second_day=True))
        restored = deserialize_project_projection(
            serialize_project_projection(original)
        )

        self.assertEqual(restored, original)
        self.assertEqual(
            project_projection_fingerprint(restored),
            project_projection_fingerprint(original),
        )

    def test_delta_rejects_different_weeks(self) -> None:
        previous = projection(project())
        current = ProjectCommunicationProjection(
            week_start=date(2026, 9, 28),
            week_end=date(2026, 10, 4),
            projects=(project(),),
        )

        with self.assertRaises(ValueError):
            compare_project_communication_projections(previous, current)

    def test_delta_only_creates_draft_for_affected_project(self) -> None:
        stable = project(project_id="P1", number="1000")
        changed_before = project(project_id="P2", number="2000")
        changed_after = project(
            project_id="P2",
            number="2000",
            task_description="Nouvelle tâche",
        )
        previous = projection(stable, changed_before)
        current = projection(stable, changed_after)

        changes = compare_project_communication_projections(previous, current)
        self.assertEqual(
            [(row.project_id, row.kind) for row in changes],
            [("P2", "modified")],
        )
        batch = build_project_delta_batch(previous, current)
        self.assertEqual([draft.message_key for draft in batch.drafts], ["project:P2"])
        self.assertEqual(batch.drafts[0].message_kind, MESSAGE_KIND_PLANNING_CHANGE)
        self.assertIn("Mise à jour du planning communiqué.", batch.drafts[0].body)

    def test_internal_shift_id_change_does_not_create_delta(self) -> None:
        before_resource = resource("R1", "Alice", address=email("alice"))
        after_resource = ProjectCommunicationResource(
            resource_id=before_resource.resource_id,
            resource_name=before_resource.resource_name,
            contact=before_resource.contact,
            hours=before_resource.hours,
            shift_ids=("S-REBUILT",),
            allocation_types=before_resource.allocation_types,
            confirmations=before_resource.confirmations,
            outside_schedule=before_resource.outside_schedule,
        )

        changes = compare_project_communication_projections(
            projection(project(resources=(before_resource,))),
            projection(project(resources=(after_resource,))),
        )

        self.assertEqual(changes, ())

    def test_same_resource_contact_change_prefers_current_email_in_delta_cc(self) -> None:
        before = project(
            resources=(resource("R1", "Alice", address=email("alice-old")),)
        )
        after = project(
            resources=(resource("R1", "Alice", address=email("alice-new")),)
        )
        draft = build_project_delta_batch(
            projection(before),
            projection(after),
        ).drafts[0]

        self.assertEqual(
            [row.email for row in draft.cc_recipients],
            [email("alice-new")],
        )

    def test_resource_change_delta_ccs_old_and_new_resources(self) -> None:
        before = project(
            resources=(resource("R1", "Alice", address=email("alice")),)
        )
        after = project(
            resources=(resource("R2", "Bob", address=email("bob")),)
        )
        draft = build_project_delta_batch(
            projection(before),
            projection(after),
        ).drafts[0]

        self.assertEqual(
            {row.email for row in draft.cc_recipients},
            {email("alice"), email("bob")},
        )
        self.assertNotIn("Alice", draft.body)
        self.assertIn("Bob", draft.body)

    def test_no_standalone_technician_message_is_generated(self) -> None:
        batch = build_project_confirmation_batch(projection(project()))
        self.assertTrue(batch.drafts)
        self.assertEqual({draft.audience for draft in batch.drafts}, {"project"})


if __name__ == "__main__":
    unittest.main()
