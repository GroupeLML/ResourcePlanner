from __future__ import annotations

from datetime import date
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from app.domain.operational_contacts import ContactResolution, STATUS_RESOLVED
from app.domain.project_communication import (
    ProjectCommunicationAsset,
    ProjectCommunicationDay,
    ProjectCommunicationParticipant,
    ProjectCommunicationProject,
    ProjectCommunicationProjection,
    ProjectCommunicationResource,
    ProjectCommunicationRequestAssignment,
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
    render_project_review_html,
    serialize_project_projection,
)


WEEK = date(2026, 9, 21)


def synthetic_phone(last_four: str = "0100") -> str:
    return "514" + "-" + "555" + "-" + last_four


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
    assets: tuple[ProjectCommunicationAsset, ...] = (),
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
        assets=assets,
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
    requesters: tuple[ProjectCommunicationParticipant, ...] = (),
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
        requesters=requesters,
    )


def projection(*projects: ProjectCommunicationProject) -> ProjectCommunicationProjection:
    # Give each synthetic physical Shift a unique identity. Resource fixtures
    # are reused across dates and projects, but those are distinct real Shifts.
    seen_shifts: set[str] = set()
    complete_projects = []
    for source in projects:
        days = []
        for day in source.days:
            tasks = []
            for index, task in enumerate(day.tasks):
                resources = []
                provenance = []
                shift_replacements: dict[str, str] = {}
                for row in task.resources:
                    new_shift_ids = []
                    for original_shift_id in row.shift_ids:
                        shift_id = original_shift_id
                        if shift_id in seen_shifts:
                            shift_id = (
                                f"{original_shift_id}:{source.project_id}:"
                                f"{day.day.isoformat()}:{index}"
                            )
                        if shift_id in seen_shifts:
                            raise AssertionError("Synthetic Shift identity is not unique")
                        seen_shifts.add(shift_id)
                        shift_replacements[original_shift_id] = shift_id
                        new_shift_ids.append(shift_id)
                        provenance.append(ProjectCommunicationRequestAssignment(
                            shift_id=shift_id,
                            requirement_id="REQ-1",
                            workforce_request_id="DEMAND-1",
                            request_number="DMO-1",
                            resource_id=row.resource_id,
                            resource_name=row.resource_name,
                            hours=row.hours / len(row.shift_ids),
                            site_contact_name=None,
                            site_contact_phone=None,
                        ))
                    resources.append(replace(row, shift_ids=tuple(new_shift_ids)))
                responsibility_assignments = tuple(
                    replace(item, shift_id=shift_replacements.get(item.shift_id, item.shift_id))
                    for item in task.responsibility_assignments
                )
                tasks.append(replace(
                    task,
                    resources=tuple(resources),
                    responsibility_assignments=responsibility_assignments,
                    request_assignments=tuple(provenance),
                ))
            days.append(replace(day, tasks=tuple(tasks)))
        complete_projects.append(replace(source, days=tuple(days)))
    return ProjectCommunicationProjection(
        week_start=WEEK,
        week_end=date(2026, 9, 27),
        projects=tuple(complete_projects),
        request_provenance_version=1,
        request_provenance_complete=True,
    )

class ProjectCommunicationMessageTests(unittest.TestCase):
    def test_855_semantic_renderer_emits_corresponding_escaped_html(self) -> None:
        source = replace(
            project(resources=(resource("R1", "Alice <Tech>", address=email("alice")),)),
            project_name="Projet <Alpha> & Beta",
        )
        draft = build_project_confirmation_batch(projection(source)).drafts[0]

        self.assertIn("Projet <Alpha> & Beta", draft.body)
        self.assertIn("Alice <Tech>", draft.body)
        self.assertIsNotNone(draft.body_html)
        assert draft.body_html is not None
        self.assertIn("Projet &lt;Alpha&gt; &amp; Beta", draft.body_html)
        self.assertIn("Alice &lt;Tech&gt;", draft.body_html)
        self.assertNotIn("Projet <Alpha>", draft.body_html)
        self.assertEqual(
            render_project_review_html("Texte <édité> & sûr"),
            "<div><div>Texte &lt;édité&gt; &amp; sûr</div></div>",
        )

    def test_855_html_change_changes_message_content_fingerprint(self) -> None:
        source = projection(
            project(resources=(resource("R1", "Alice", address=email("alice")),))
        )
        first = build_project_confirmation_batch(source).drafts[0]
        with patch(
            "app.domain.project_communication_messages._render_project_html",
            return_value="<div>alternate-html</div>",
        ):
            second = build_project_confirmation_batch(source).drafts[0]

        self.assertEqual(first.body, second.body)
        self.assertNotEqual(first.body_html, second.body_html)
        self.assertNotEqual(
            first.content_fingerprint,
            second.content_fingerprint,
        )

    def test_resource_line_without_asset_is_unchanged(self) -> None:
        batch = build_project_confirmation_batch(
            projection(
                project(
                    resources=(
                        resource("R1", "Alice", address=email("alice")),
                    )
                )
            )
        )
        body = batch.drafts[0].body
        self.assertIn("  • Alice — 8 h", body)
        self.assertNotIn("Actif :", body)

    def test_resource_line_renders_multiple_assets_with_canonical_unit_codes(self) -> None:
        vehicle = ProjectCommunicationAsset(
            allocation_id="ALLOC-V",
            asset_id="ASSET-V",
            asset_code="V-12",
            asset_label="Camion 12",
            asset_type_label="Véhicule",
            asset_category="VEHICLE",
            association_kind="INHERITED_RESOURCE_PERIOD",
        )
        equipment = ProjectCommunicationAsset(
            allocation_id="ALLOC-E",
            asset_id="ASSET-E",
            asset_code="EQ-7",
            asset_label="Nacelle 7",
            asset_type_label="Équipement",
            asset_category="EQUIPMENT",
            association_kind="OWNED_SHIFT",
        )
        batch = build_project_confirmation_batch(
            projection(
                project(
                    resources=(
                        resource(
                            "R1",
                            "Alice",
                            address=email("alice"),
                            assets=(vehicle, equipment),
                        ),
                    )
                )
            )
        )
        body = batch.drafts[0].body
        self.assertIn("Actif : V-12 / EQ-7", body)
        self.assertNotIn("Camion 12", body)
        self.assertNotIn("Nacelle 7", body)
        self.assertNotIn("Véhicule —", body)
        self.assertNotIn("Équipement —", body)

        restored = deserialize_project_projection(
            serialize_project_projection(
                projection(
                    project(
                        resources=(
                            resource(
                                "R1",
                                "Alice",
                                address=email("alice"),
                                assets=(vehicle, equipment),
                            ),
                        )
                    )
                )
            )
        )
        restored_assets = restored.projects[0].days[0].tasks[0].resources[0].assets
        self.assertEqual(restored_assets, (vehicle, equipment))

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

    def test_requester_is_additive_to_manager_and_existing_cc_recipients(self) -> None:
        requester = participant(
            "Demandeuse",
            contact_id="C-REQ",
            user_id="U-REQ",
            address=email("requester"),
        )
        draft = build_project_confirmation_batch(
            projection(project(requesters=(requester,)))
        ).drafts[0]

        self.assertEqual(
            [row.email for row in draft.to_recipients],
            [email("pm"), email("requester")],
        )
        self.assertEqual(
            {row.email for row in draft.cc_recipients},
            {email("alice"), email("bob")},
        )

        restored = deserialize_project_projection(
            serialize_project_projection(
                projection(project(requesters=(requester,)))
            )
        )
        self.assertEqual(restored.projects[0].requesters, (requester,))

    def test_requester_same_as_project_manager_is_not_duplicated_in_to(self) -> None:
        requester = participant(
            "Même personne",
            contact_id="C-PM",
            user_id="U-PM",
            address=email("pm"),
        )
        draft = build_project_confirmation_batch(
            projection(project(requesters=(requester,)))
        ).drafts[0]

        self.assertEqual(
            [row.email for row in draft.to_recipients],
            [email("pm")],
        )

    def test_requester_same_email_is_deduplicated_without_display_name_matching(self) -> None:
        requester = participant(
            "Nom complètement différent",
            contact_id="C-REQ-DIFFERENT",
            user_id="U-REQ-DIFFERENT",
            address=email("pm"),
        )
        draft = build_project_confirmation_batch(
            projection(project(requesters=(requester,)))
        ).drafts[0]

        self.assertEqual(
            [row.email for row in draft.to_recipients],
            [email("pm")],
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

        duplicate = participant(
            "Autre identité",
            contact_id="C-CO-DUP",
            user_id="U-CO-DUP",
            address=email("pm"),
        )
        duplicate_to_draft = build_project_confirmation_batch(
            projection(project(co_managers=(duplicate,)))
        ).drafts[0]
        self.assertEqual(
            [row.email for row in duplicate_to_draft.to_recipients],
            [email("pm")],
        )

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

        missing_co_manager = participant(
            "Co chargé sans courriel",
            contact_id="C-CO-NO-MAIL",
            user_id="U-CO-NO-MAIL",
            address=None,
        )
        co_manager_draft = build_project_confirmation_batch(
            projection(project(co_managers=(missing_co_manager,)))
        ).drafts[0]
        co_manager_diagnostic = next(
            row
            for row in co_manager_draft.diagnostics
            if row.code == DIAGNOSTIC_CO_MANAGER_TO_EMAIL_MISSING
        )
        self.assertEqual(co_manager_diagnostic.entity_type, "project_co_manager")
        self.assertEqual(co_manager_diagnostic.entity_id, "C-CO-NO-MAIL")
        self.assertEqual(co_manager_diagnostic.severity, SEVERITY_BLOCKING)
        self.assertFalse(co_manager_draft.approvable)

        restored = deserialize_project_projection(
            serialize_project_projection(
                projection(project(co_managers=(missing_co_manager,)))
            )
        )
        self.assertEqual(
            restored.projects[0].project_co_managers[0].contact_id,
            "C-CO-NO-MAIL",
        )

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

    def _830_single_demand_projection(
        self,
        *,
        name: str | None,
        phone: str | None,
        two_shifts: bool = False,
    ) -> ProjectCommunicationProjection:
        asset = ProjectCommunicationAsset(
            allocation_id="ALLOC-162",
            asset_id="ASSET-162",
            asset_code="MAT162",
            asset_label="Unit 162",
            asset_type_label="Equipment",
            asset_category="EQUIPMENT",
            association_kind="INHERITED_REQUEST",
        )
        person = resource(
            "R654", "654 - SHAWN FRAPPIER-BARABÉ",
            address=email("employee"), hours=8.0,
            confirmation="Tentative", assets=(asset,),
        )
        base = projection(project(resources=(person,)))
        source = base.projects[0]
        day = source.days[0]
        task = day.tasks[0]
        row = task.request_assignments[0]
        if two_shifts:
            provenance = (
                replace(row, shift_id="S-A", requirement_id="REQ-A", hours=3.0),
                replace(row, shift_id="S-B", requirement_id="REQ-B", hours=5.0),
            )
            employee = replace(task.resources[0], shift_ids=("S-A", "S-B"))
        else:
            provenance = (row,)
            employee = task.resources[0]
        provenance = tuple(
            replace(item, workforce_request_id="DEMAND-124",
                    request_number="DMO-2026-0124",
                    site_contact_name=name, site_contact_phone=phone)
            for item in provenance
        )
        modified_task = replace(
            task, resources=(employee,), request_assignments=provenance
        )
        return replace(
            base,
            projects=(replace(
                source, days=(replace(day, tasks=(modified_task,)),)
            ),),
        )

    def test_866_single_demand_without_contact_stays_compact(self) -> None:
        current = self._830_single_demand_projection(name=None, phone=None)
        batch = build_project_confirmation_batch(current)
        body = batch.drafts[0].body
        self.assertIn(
            "  • 654 - SHAWN FRAPPIER-BARABÉ — 8 h (Tentative) · Actif : MAT162",
            body,
        )
        self.assertEqual(body.count("• 654 - SHAWN FRAPPIER-BARABÉ"), 1)
        self.assertNotIn("Demande DMO-2026-0124", body)
        self.assertNotIn("part de la demande", body)
        self.assertNotIn("Totaux planifiés", body)

    def test_866_single_demand_contact_variants_show_one_canonical_line(self) -> None:
        for name, phone, expected in (
            ("Bob Barker", "5149370707", "Bob Barker — 5149370707"),
            ("Bob Barker", None, "Bob Barker"),
            (None, "5149370707", "5149370707"),
        ):
            with self.subTest(name=name, phone=phone):
                current = self._830_single_demand_projection(
                    name=name, phone=phone
                )
                draft = build_project_confirmation_batch(current).drafts[0]
                self.assertIn(
                    "Demande DMO-2026-0124\n  Contact chantier : "
                    + expected + "\n  • 654 - SHAWN FRAPPIER-BARABÉ",
                    draft.body,
                )
                self.assertEqual(
                    draft.body.count("• 654 - SHAWN FRAPPIER-BARABÉ"), 1
                )
                self.assertIn("— 8 h (Tentative) · Actif : MAT162", draft.body)
                self.assertNotIn("part de la demande", draft.body)
                self.assertNotIn("Totaux planifiés", draft.body)

    def test_866_two_physical_shifts_same_demand_stay_one_line(self) -> None:
        current = self._830_single_demand_projection(
            name="Bob Barker", phone="5149370707", two_shifts=True
        )
        task = current.projects[0].days[0].tasks[0]
        self.assertEqual({row.shift_id for row in task.request_assignments}, {"S-A", "S-B"})
        self.assertEqual(
            {row.workforce_request_id for row in task.request_assignments},
            {"DEMAND-124"},
        )
        draft = build_project_confirmation_batch(current).drafts[0]
        self.assertEqual(draft.body.count("• 654 - SHAWN FRAPPIER-BARABÉ"), 1)
        self.assertIn("8 h (Tentative) · Actif : MAT162", draft.body)
        self.assertNotIn("part de la demande", draft.body)
        self.assertNotIn("Totaux planifiés", draft.body)

    def test_866_mixed_demand_and_adhoc_retains_distinct_contributions(self) -> None:
        current = self._830_single_demand_projection(
            name="Bob Barker", phone="5149370707", two_shifts=True
        )
        source = current.projects[0]
        day = source.days[0]
        task = day.tasks[0]
        first = replace(task.request_assignments[0], hours=4.0)
        adhoc = replace(
            task.request_assignments[1],
            workforce_request_id=None, request_number=None,
            site_contact_name=None, site_contact_phone=None, hours=4.0,
        )
        modified_task = replace(task, request_assignments=(first, adhoc))
        mixed = replace(
            current,
            projects=(replace(
                source, days=(replace(day, tasks=(modified_task,)),)
            ),),
        )
        body = build_project_confirmation_batch(mixed).drafts[0].body
        self.assertIn(
            "Demande DMO-2026-0124\n  Contact chantier : Bob Barker — 5149370707"
            "\n  ↳ 654 - SHAWN FRAPPIER-BARABÉ — 4 h (part de la demande)", body,
        )
        self.assertIn(
            "Sans demande\n  ↳ 654 - SHAWN FRAPPIER-BARABÉ — 4 h", body
        )
        self.assertEqual(body.count("Totaux planifiés (toutes demandes)"), 1)
        self.assertEqual(body.count("  • 654 - SHAWN FRAPPIER-BARABÉ — 8 h"), 1)
        self.assertIn("Actif : MAT162", body)
        self.assertNotIn("Sans demande\n  Contact chantier :", body)

    def test_866_rendering_preserves_p1_semantics_and_serialized_preview(self) -> None:
        current = self._830_single_demand_projection(
            name="Bob Barker", phone="5149370707", two_shifts=True
        )
        initial_fingerprint = project_projection_fingerprint(current)
        serialized = serialize_project_projection(current)
        restored = deserialize_project_projection(serialized)
        self.assertEqual(project_projection_fingerprint(restored), initial_fingerprint)
        first = build_project_confirmation_batch(current)
        prepared = build_project_confirmation_batch(restored)
        self.assertEqual(first.snapshot_fingerprint, initial_fingerprint)
        self.assertEqual(prepared.snapshot_fingerprint, initial_fingerprint)
        self.assertEqual(first.drafts[0].body, prepared.drafts[0].body)
        self.assertEqual(
            first.drafts[0].content_fingerprint,
            prepared.drafts[0].content_fingerprint,
        )
        self.assertEqual(first.drafts[0].message_key, "project:P1")
        self.assertEqual(first.drafts[0].to_recipients, prepared.drafts[0].to_recipients)

    def test_855_v2_round_trip_mapping_fingerprint_and_cross_version_policy(self) -> None:
        first_asset = ProjectCommunicationAsset(
            allocation_id="ALLOC-151",
            asset_id="ASSET-151",
            asset_code="MAT151",
            asset_label="Unité 151",
            asset_type_label="Véhicule",
            asset_category="VEHICLE",
            association_kind="OWNED_SHIFT",
        )
        second_asset = ProjectCommunicationAsset(
            allocation_id="ALLOC-204",
            asset_id="ASSET-204",
            asset_code="MAT204",
            asset_label="Unité 204",
            asset_type_label="Véhicule",
            asset_category="VEHICLE",
            association_kind="INHERITED_REQUEST",
        )
        row = replace(
            resource("R1", "Alice", address=email("alice"), hours=8.0),
            shift_ids=("S-A", "S-B"),
            assets=(first_asset, second_asset),
        )
        base = projection(project(resources=(row,)))
        project_row = base.projects[0]
        day = project_row.days[0]
        task = day.tasks[0]
        original_assignments = task.request_assignments
        self.assertEqual(len(original_assignments), 2)

        mapped = (
            replace(
                original_assignments[0],
                hours=3.0,
                assets=(first_asset,),
                standard_hours=8.0,
            ),
            replace(
                original_assignments[1],
                hours=5.0,
                assets=(second_asset,),
                standard_hours=8.0,
            ),
        )
        v2 = replace(
            base,
            projects=(
                replace(
                    project_row,
                    days=(
                        replace(
                            day,
                            tasks=(replace(task, request_assignments=mapped),),
                        ),
                    ),
                ),
            ),
            request_provenance_version=2,
            request_provenance_complete=True,
        )
        restored = deserialize_project_projection(
            serialize_project_projection(v2)
        )
        self.assertEqual(restored, v2)
        self.assertEqual(
            project_projection_fingerprint(restored),
            project_projection_fingerprint(v2),
        )

        swapped = replace(
            v2,
            projects=(
                replace(
                    project_row,
                    days=(
                        replace(
                            day,
                            tasks=(
                                replace(
                                    task,
                                    request_assignments=(
                                        replace(
                                            mapped[0],
                                            assets=(second_asset,),
                                        ),
                                        replace(
                                            mapped[1],
                                            assets=(first_asset,),
                                        ),
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )
        self.assertNotEqual(
            project_projection_fingerprint(v2),
            project_projection_fingerprint(swapped),
        )

        v1 = replace(
            base,
            request_provenance_version=1,
            request_provenance_complete=True,
        )
        v1_payload = json.loads(serialize_project_projection(v1))
        request_row = v1_payload["projects"][0]["days"][0]["tasks"][0][
            "request_assignments"
        ][0]
        self.assertNotIn("assets", request_row)
        self.assertNotIn("standard_hours", request_row)
        self.assertEqual(
            len(compare_project_communication_projections(v1, v2)),
            1,
        )
        with self.assertRaisesRegex(ValueError, "Régression"):
            compare_project_communication_projections(v2, v1)

        malformed = json.loads(serialize_project_projection(v2))
        del malformed["projects"][0]["days"][0]["tasks"][0][
            "request_assignments"
        ][0]["standard_hours"]
        with self.assertRaisesRegex(ValueError, "incomplet"):
            deserialize_project_projection(
                json.dumps(malformed, ensure_ascii=False)
            )

    def test_831_erp_manager_phone_renders_only_when_available(self) -> None:
        base = projection(project())
        original = base.projects[0]
        self.assertIsNone(original.project_manager_erp_phone)
        original_body = build_project_confirmation_batch(base).drafts[0].body
        self.assertNotIn("Chargé de projet :", original_body)
        self.assertNotIn("Téléphone :", original_body)

        for number in (synthetic_phone("0100"), synthetic_phone("0200")):
            with self.subTest(number=number):
                current = replace(
                    base,
                    projects=(replace(original, project_manager_erp_phone=number),),
                )
                batch = build_project_confirmation_batch(current)
                body = batch.drafts[0].body
                self.assertIn(
                    "Chargé de projet : Chargé Démo\nTéléphone : " + number,
                    body,
                )
                self.assertIn("Responsable : Jean Responsable", body)
                self.assertNotIn("Contact chantier :", body)
                self.assertEqual(len(batch.drafts), 1)
                self.assertEqual(batch.drafts[0].to_recipients,
                                 build_project_confirmation_batch(base).drafts[0].to_recipients)
                self.assertEqual(batch.drafts[0].cc_recipients,
                                 build_project_confirmation_batch(base).drafts[0].cc_recipients)

    def test_831_phone_changes_p1_and_delta_without_legacy_null_changes(self) -> None:
        original = projection(project())
        p = original.projects[0]
        with_phone = replace(
            original, projects=(replace(p, project_manager_erp_phone=synthetic_phone("0100")),)
        )
        changed = replace(
            original, projects=(replace(p, project_manager_erp_phone=synthetic_phone("0200")),)
        )
        original_fingerprint = project_projection_fingerprint(original)
        self.assertNotEqual(project_projection_fingerprint(with_phone), original_fingerprint)
        self.assertNotEqual(project_projection_fingerprint(changed),
                            project_projection_fingerprint(with_phone))
        self.assertEqual(len(compare_project_communication_projections(original, with_phone)), 1)
        self.assertEqual(len(compare_project_communication_projections(with_phone, changed)), 1)
        self.assertEqual(len(compare_project_communication_projections(with_phone, original)), 1)

        # The absent-value representation is exactly the historical project_v2
        # structure: restoring the old snapshot must not introduce a new P1 input.
        legacy_json = serialize_project_projection(original)
        self.assertNotIn("project_manager_erp_phone", legacy_json)
        restored = deserialize_project_projection(legacy_json)
        self.assertIsNone(restored.projects[0].project_manager_erp_phone)
        self.assertEqual(serialize_project_projection(restored), legacy_json)
        self.assertEqual(project_projection_fingerprint(restored), original_fingerprint)

        encoded = serialize_project_projection(with_phone)
        self.assertIn('"project_manager_erp_phone":"' + synthetic_phone("0100") + '"', encoded)
        restored_phone = deserialize_project_projection(encoded)
        self.assertEqual(restored_phone, with_phone)
        self.assertEqual(project_projection_fingerprint(restored_phone),
                         project_projection_fingerprint(with_phone))

    def test_831_phone_keeps_request_contact_and_single_demand_resource_once(self) -> None:
        base = projection(
            project(resources=(resource("R1", "Alice", address=email("alice")),))
        )
        p = base.projects[0]
        day = p.days[0]
        task = day.tasks[0]
        row = task.request_assignments[0]
        task = replace(task, request_assignments=(
            replace(row, site_contact_name="Chantier", site_contact_phone="555-0300"),
        ))
        current = replace(
            base,
            projects=(replace(p, project_manager_erp_phone=synthetic_phone("0100"),
                              days=(replace(day, tasks=(task,)),)),),
        )
        draft = build_project_confirmation_batch(current).drafts[0]
        self.assertIn("Téléphone : " + synthetic_phone("0100"), draft.body)
        self.assertIn("Contact chantier : Chantier — 555-0300", draft.body)
        self.assertEqual(draft.body.count("  • Alice — 8 h"), 1)
        self.assertNotIn("part de la demande", draft.body)
        self.assertNotIn("Totaux planifiés", draft.body)

    def test_830_contact_add_change_clear_and_legacy_delta(self) -> None:
        base = projection(project())
        old_project = base.projects[0]
        task = old_project.days[0].tasks[0]
        def with_contact(name, phone):
            changed = replace(
                task,
                request_assignments=tuple(
                    replace(row, site_contact_name=name, site_contact_phone=phone)
                    for row in task.request_assignments
                ),
            )
            day = replace(old_project.days[0], tasks=(changed,))
            return replace(base, projects=(replace(old_project, days=(day,)),))

        added = with_contact("Jean Tremblay", "555-0101")
        changed = with_contact("Pierre Gagnon", "555-0202")
        self.assertEqual(len(compare_project_communication_projections(base, added)), 1)
        self.assertEqual(len(compare_project_communication_projections(added, changed)), 1)
        self.assertEqual(len(compare_project_communication_projections(added, base)), 1)
        self.assertIn("Contact chantier", build_project_confirmation_batch(added).drafts[0].body)
        self.assertEqual(
            project_projection_fingerprint(
                deserialize_project_projection(serialize_project_projection(added))
            ),
            project_projection_fingerprint(added),
        )
        legacy = replace(
            base,
            projects=(replace(old_project, days=(
                replace(old_project.days[0], tasks=(replace(task, request_assignments=()),)),
            )),),
            request_provenance_version=None,
            request_provenance_complete=False,
        )
        self.assertEqual(compare_project_communication_projections(legacy, base), ())
        self.assertEqual(len(compare_project_communication_projections(legacy, added)), 1)

    def test_830_request_specific_hours_render_2_6_and_6_2_distinctly(self) -> None:
        base = projection(
            project(resources=(resource("R1", "Alice", address=email("alice")),))
        )
        old_project = base.projects[0]
        day = old_project.days[0]
        task = day.tasks[0]
        original = task.resources[0]

        def allocate(first_hours: float, second_hours: float):
            a = replace(
                task.request_assignments[0],
                shift_id="S-A",
                requirement_id="REQ-A",
                workforce_request_id="DEMAND-A",
                request_number="DM-A",
                hours=first_hours,
                site_contact_name="Jean",
                site_contact_phone="555-0101",
            )
            b = replace(
                task.request_assignments[0],
                shift_id="S-B",
                requirement_id="REQ-B",
                workforce_request_id="DEMAND-B",
                request_number="DM-B",
                hours=second_hours,
                site_contact_name="Pierre",
                site_contact_phone="555-0202",
            )
            adjusted = replace(
                task,
                resources=(replace(original, shift_ids=("S-A", "S-B")),),
                request_assignments=(a, b),
            )
            changed_day = replace(day, tasks=(adjusted,))
            return replace(
                base,
                projects=(replace(old_project, days=(changed_day,)),),
            )

        two_six = allocate(2.0, 6.0)
        six_two = allocate(6.0, 2.0)
        drafts = (
            build_project_confirmation_batch(two_six).drafts,
            build_project_confirmation_batch(six_two).drafts,
        )
        self.assertEqual(len(drafts[0]), 1)
        self.assertEqual(len(drafts[1]), 1)
        one = drafts[0][0]
        two = drafts[1][0]
        self.assertEqual(one.message_key, "project:P1")
        self.assertEqual(one.to_recipients, two.to_recipients)
        self.assertEqual(one.cc_recipients, two.cc_recipients)
        self.assertIn("Lundi le 21 septembre 2026 — Installation de poteaux", one.body)
        self.assertIn("Demande DM-A\n  Contact chantier : Jean — 555-0101\n  ↳ Alice — 2 h", one.body)
        self.assertIn("Demande DM-B\n  Contact chantier : Pierre — 555-0202\n  ↳ Alice — 6 h", one.body)
        self.assertIn("Demande DM-A\n  Contact chantier : Jean — 555-0101\n  ↳ Alice — 6 h", two.body)
        self.assertIn("Demande DM-B\n  Contact chantier : Pierre — 555-0202\n  ↳ Alice — 2 h", two.body)
        self.assertIn("  • Alice — 8 h", one.body)
        self.assertIn("  • Alice — 8 h", two.body)
        self.assertNotEqual(one.body, two.body)
        self.assertNotEqual(project_projection_fingerprint(two_six), project_projection_fingerprint(six_two))
        self.assertEqual(
            deserialize_project_projection(
                serialize_project_projection(two_six)
            ), two_six,
        )

    def test_830_different_employees_shared_contacts_and_adhoc_work(self) -> None:
        alice = resource("R1", "Alice", address=email("alice"), hours=2)
        bob = resource("R2", "Bob", address=email("bob"), hours=6)
        carol = resource("R3", "Carol", address=email("carol"), hours=1)
        base = projection(project(resources=(alice, bob, carol)))
        task = base.projects[0].days[0].tasks[0]
        by_id = {row.resource_id: row for row in task.request_assignments}
        provenance = (
            replace(by_id["R1"], workforce_request_id="REQ-A", request_number="DM-A",
                    site_contact_name="Jean", site_contact_phone="555-0101"),
            replace(by_id["R2"], workforce_request_id="REQ-B", request_number="DM-B",
                    site_contact_name="Jean", site_contact_phone="555-0101"),
            replace(by_id["R3"], workforce_request_id=None, request_number=None,
                    site_contact_name=None, site_contact_phone=None),
        )
        updated_task = replace(task, request_assignments=provenance)
        updated_day = replace(base.projects[0].days[0], tasks=(updated_task,))
        current = replace(
            base,
            projects=(replace(base.projects[0], days=(updated_day,)),),
        )
        draft = build_project_confirmation_batch(current).drafts[0]
        self.assertIn("Demande DM-A\n  Contact chantier : Jean — 555-0101\n  ↳ Alice — 2 h", draft.body)
        self.assertIn("Demande DM-B\n  Contact chantier : Jean — 555-0101\n  ↳ Bob — 6 h", draft.body)
        self.assertIn("Sans demande\n  ↳ Carol — 1 h", draft.body)
        self.assertEqual(draft.body.count("Contact chantier : Jean"), 2)
        self.assertEqual(len(build_project_confirmation_batch(current).drafts), 1)
        self.assertEqual({row.email for row in draft.cc_recipients}, {email("alice"), email("bob"), email("carol")})
        self.assertIn("  • Alice — 2 h", draft.body)
        self.assertIn("  • Bob — 6 h", draft.body)
        self.assertIn("  • Carol — 1 h", draft.body)

    def test_830_version_and_completeness_fail_closed(self) -> None:
        current = projection(project())
        encoded = json.loads(serialize_project_projection(current))
        encoded["request_provenance_version"] = 2
        with self.assertRaises(ValueError):
            deserialize_project_projection(json.dumps(encoded))
        encoded["request_provenance_version"] = 1
        encoded["projects"][0]["days"][0]["tasks"][0]["request_assignments"] = []
        with self.assertRaises(ValueError):
            deserialize_project_projection(json.dumps(encoded))
        encoded.pop("request_provenance_version")
        with self.assertRaises(ValueError):
            deserialize_project_projection(json.dumps(encoded))

    def test_no_standalone_technician_message_is_generated(self) -> None:
        batch = build_project_confirmation_batch(projection(project()))
        self.assertTrue(batch.drafts)
        self.assertEqual({draft.audience for draft in batch.drafts}, {"project"})


if __name__ == "__main__":
    unittest.main()
