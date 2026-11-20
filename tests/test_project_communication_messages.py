from __future__ import annotations

from datetime import date
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from app.domain.operational_contacts import (
    ContactResolution,
    SOURCE_PROJECT_MANAGER,
    STATUS_RESOLVED,
)
from app.domain.project_communication import (
    ProjectCommunicationAsset,
    ProjectCommunicationAssignment,
    ProjectCommunicationDay,
    ProjectCommunicationParticipant,
    ProjectCommunicationProject,
    ProjectCommunicationProjection,
    ProjectCommunicationResource,
    ProjectCommunicationRequestAssignment,
    ProjectCommunicationResponsibilityAssignment,
    ProjectCommunicationTask,
    build_project_communication_projection,
)
from app.domain.project_communication_messages import (
    DIAGNOSTIC_CC_EMAIL_MISSING,
    DIAGNOSTIC_CO_MANAGER_TO_EMAIL_MISSING,
    DIAGNOSTIC_TO_EMAIL_MISSING,
    MESSAGE_KIND_PLANNING_CHANGE,
    MESSAGE_KIND_WEEKLY_CONFIRMATION,
    PROJECT_RESPONSIBILITY_FOOTER,
    SEVERITY_BLOCKING,
    SEVERITY_WARNING,
    build_project_confirmation_batch,
    build_project_delta_batch,
    canonical_project_recipients,
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

def v4_shift_projection(
    shifts: tuple[dict[str, object], ...],
    *,
    project_id: str = "P1",
    project_number: str = "1000",
    project_name: str | None = None,
    manager_local: str = "pm",
) -> ProjectCommunicationProjection:
    rows = []
    manager = participant(
        f"Chargé {project_number}",
        contact_id=f"C-PM-{project_id}",
        user_id=f"U-PM-{project_id}",
        address=email(manager_local),
    )
    for value in shifts:
        resource_id = str(value["resource_id"])
        resource_name = str(value["resource_name"])
        rows.append(
            ProjectCommunicationAssignment(
                shift_id=str(value["shift_id"]),
                requirement_id=str(value.get("requirement_id") or f"REQ-{value['shift_id']}"),
                project_id=project_id,
                project_number=project_number,
                project_name=project_name or f"Projet {project_number}",
                day=value.get("day", WEEK),
                hours=float(value.get("hours", 8.0)),
                allocation_type=str(value.get("allocation_type", "Flexible")),
                outside_schedule=value.get("outside_schedule", False),
                confirmation=str(value.get("confirmation", "Confirmée")),
                task_id=value.get("task_id", "TASK-889"),
                task_code=value.get("task_code", "210"),
                task_description=str(value.get("task_description", "Travaux #889")),
                resource_id=resource_id,
                resource_name=resource_name,
                resource_contact=participant(
                    resource_name,
                    contact_id=f"C-{resource_id}",
                    user_id=f"U-{resource_id}",
                    address=email(resource_id.lower()),
                ),
                project_manager=manager,
                operational_responsible=responsible(
                    contact_id=f"C-RESP-{project_id}",
                    name=f"Responsable {project_number}",
                ),
                workforce_request_id=str(value.get("workforce_request_id", "D-889")),
                request_number=str(value.get("request_number", "DMO-889")),
                site_contact_name=value.get("site_contact_name"),
                site_contact_phone=value.get("site_contact_phone"),
                include_project_manager_contact_phone=False,
                project_manager_contact_phone=None,
                assets=tuple(value.get("assets", ())),
                standard_hours=float(value.get("standard_hours", 8.0)),
            )
        )
    return build_project_communication_projection(
        week_start=WEEK,
        week_end=date(2026, 9, 27),
        assignments=tuple(rows),
        request_provenance_version=4,
    )


def historical_version(
    current: ProjectCommunicationProjection,
    version: int,
) -> ProjectCommunicationProjection:
    projects = []
    for project_row in current.projects:
        days = []
        for day in project_row.days:
            tasks = []
            for task in day.tasks:
                assignments = tuple(
                    replace(
                        row,
                        confirmation=None,
                        allocation_type=None,
                        outside_schedule=None,
                        task_id=None,
                        task_code=None,
                        task_description=None,
                        include_project_manager_contact_phone=(
                            row.include_project_manager_contact_phone
                            if version >= 3
                            else False
                        ),
                        project_manager_contact_phone=(
                            row.project_manager_contact_phone
                            if version >= 3
                            else None
                        ),
                        assets=(row.assets if version >= 2 else ()),
                        standard_hours=(
                            row.standard_hours if version >= 2 else None
                        ),
                    )
                    for row in task.request_assignments
                )
                tasks.append(replace(task, request_assignments=assignments))
            days.append(replace(day, tasks=tuple(tasks)))
        projects.append(replace(project_row, days=tuple(days)))
    return replace(
        current,
        projects=tuple(projects),
        request_provenance_version=version,
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
        reviewed = (
            "Bonjour <équipe> & partenaires\n\n"
            "Texte entièrement libre.\n"
            "<script>alert('x')</script>"
        )
        reviewed_html = render_project_review_html(reviewed)
        self.assertIn("Bonjour &lt;équipe&gt; &amp; partenaires", reviewed_html)
        self.assertIn("Texte entièrement libre.", reviewed_html)
        self.assertIn("&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;", reviewed_html)
        self.assertNotIn("<script>", reviewed_html)
        self.assertNotIn("<strong><u>Responsable projet</u></strong>", reviewed_html)
        self.assertTrue(reviewed_html.endswith("</div>"))
        with self.assertRaisesRegex(ValueError, "ne peut pas être vide"):
            render_project_review_html("   \n")

    def test_855_capture_authoritative_order_emphasis_footer_and_shift_rules(self) -> None:
        asset = ProjectCommunicationAsset(
            allocation_id="ALLOC-MAT151",
            asset_id="ASSET-MAT151",
            asset_code="MAT151",
            asset_label="Unité 151",
            asset_type_label="Véhicule",
            asset_category="VEHICLE",
            association_kind="OWNED_SHIFT",
        )
        manager = participant(
            "Marie-Pier Dulac",
            contact_id="C-PM-855",
            user_id="U-PM-855",
            address=email("marie-pier"),
        )
        day_one = date(2026, 11, 19)
        day_two = date(2026, 11, 20)
        david_day_one = replace(
            resource(
                "R-DAVID",
                "654 - DAVID DERY",
                address=email("david"),
                hours=8.0,
                assets=(asset,),
            ),
            shift_ids=("S-DAVID-19",),
        )
        matis_day_one = replace(
            resource(
                "R-MATIS",
                "MATIS PELLETIER",
                address=email("matis"),
                hours=8.0,
            ),
            shift_ids=("S-MATIS-19",),
        )
        task_one = ProjectCommunicationTask(
            task_description="Installation électrique pour branchement harnais (T & M)",
            task_ids=("TASK-855",),
            task_codes=("AS07112-1",),
            operational_responsibles=(),
            resources=(david_day_one, matis_day_one),
            request_assignments=(
                ProjectCommunicationRequestAssignment(
                    shift_id="S-DAVID-19",
                    requirement_id="REQ-DAVID-19",
                    workforce_request_id="REQ-855",
                    request_number="DMO-855",
                    resource_id="R-DAVID",
                    resource_name="654 - DAVID DERY",
                    hours=8.0,
                    site_contact_name="Clément",
                    site_contact_phone=synthetic_phone("0655"),
                    assets=(asset,),
                    standard_hours=8.0,
                ),
                ProjectCommunicationRequestAssignment(
                    shift_id="S-MATIS-19",
                    requirement_id="REQ-MATIS-19",
                    workforce_request_id="REQ-855",
                    request_number="DMO-855",
                    resource_id="R-MATIS",
                    resource_name="MATIS PELLETIER",
                    hours=8.0,
                    site_contact_name="Clément",
                    site_contact_phone=synthetic_phone("0655"),
                    assets=(),
                    standard_hours=8.0,
                ),
            ),
        )
        task_two = replace(
            task_one,
            resources=(
                replace(
                    david_day_one,
                    hours=5.0,
                    shift_ids=("S-DAVID-20",),
                ),
            ),
            request_assignments=(
                replace(
                    task_one.request_assignments[0],
                    shift_id="S-DAVID-20",
                    requirement_id="REQ-DAVID-20",
                    hours=5.0,
                ),
            ),
        )
        source = ProjectCommunicationProjection(
            week_start=date(2026, 11, 16),
            week_end=date(2026, 11, 22),
            projects=(
                ProjectCommunicationProject(
                    project_id="P-855",
                    project_number="M0187",
                    project_name="Rheinmetal",
                    project_manager=manager,
                    project_manager_erp_phone=synthetic_phone("0147"),
                    days=(
                        ProjectCommunicationDay(day=day_one, tasks=(task_one,)),
                        ProjectCommunicationDay(day=day_two, tasks=(task_two,)),
                    ),
                ),
            ),
            request_provenance_version=2,
            request_provenance_complete=True,
        )

        draft = build_project_confirmation_batch(source).drafts[0]
        expected = (
            "M0187 - Rheinmetal\n"
            + "\nResponsable projet\n"
            + "Marie-Pier Dulac\n"
            + synthetic_phone("0147")
            + "\n"
            + "\nDescription\n"
            + "AS07112-1 Installation électrique pour branchement harnais (T & M)\n"
            + "\nContact chantier\n"
            + "Clément : "
            + synthetic_phone("0655")
            + "\n"
            + "\nHoraire\n"
            + "\nJeudi le 19 novembre 2026\n"
            + "• DAVID DERY (MAT151)\n"
            + "• MATIS PELLETIER\n"
            + "\nVendredi le 20 novembre 2026\n"
            + "• DAVID DERY — 5 h (MAT151)\n"
            + "\n"
            + PROJECT_RESPONSIBILITY_FOOTER
        )
        self.assertEqual(draft.body, expected)
        self.assertNotIn("654 -", draft.body)
        self.assertNotIn("Actif", draft.body)
        self.assertEqual(draft.body.count(PROJECT_RESPONSIBILITY_FOOTER), 1)
        self.assertTrue(draft.body.endswith(PROJECT_RESPONSIBILITY_FOOTER))

        html = draft.body_html or ""
        self.assertIn("<strong>M0187 - Rheinmetal</strong>", html)
        for label in ("Responsable projet", "Description", "Contact chantier", "Horaire"):
            self.assertIn(f"<strong><u>{label}</u></strong>", html)
        self.assertIn("<strong>Jeudi le 19 novembre 2026</strong>", html)
        self.assertIn("<strong>Vendredi le 20 novembre 2026</strong>", html)
        self.assertIn('max-width:640px', html)
        self.assertNotIn("<table", html)
        self.assertIn("<ul ", html)
        self.assertIn("<li ", html)
        self.assertNotIn("<u>Jeudi le 19 novembre 2026</u>", html)
        self.assertNotIn(f"<strong>{PROJECT_RESPONSIBILITY_FOOTER}</strong>", html)
        self.assertIn(PROJECT_RESPONSIBILITY_FOOTER, html)

    def test_855_duration_rule_uses_snapshotted_standard_hours(self) -> None:
        base = self._830_single_demand_projection(name=None, phone=None)
        project_row = base.projects[0]
        day = project_row.days[0]
        task = day.tasks[0]
        original = task.request_assignments[0]

        for actual, standard, expected in (
            (5.0, 7.5, "— 5 h"),
            (7.5, 7.5, None),
            (9.0, 7.5, None),
            (1.0, 0.0, None),
        ):
            with self.subTest(actual=actual, standard=standard):
                changed = replace(
                    original,
                    hours=actual,
                    standard_hours=standard,
                )
                source = replace(
                    base,
                    projects=(
                        replace(
                            project_row,
                            days=(
                                replace(
                                    day,
                                    tasks=(
                                        replace(
                                            task,
                                            resources=(
                                                replace(
                                                    task.resources[0],
                                                    hours=actual,
                                                ),
                                            ),
                                            request_assignments=(changed,),
                                        ),
                                    ),
                                ),
                            ),
                        ),
                    ),
                )
                body = build_project_confirmation_batch(source).drafts[0].body
                line = next(
                    value
                    for value in body.splitlines()
                    if value.startswith("• SHAWN FRAPPIER-BARABÉ")
                )
                if expected is None:
                    self.assertNotIn(" h", line)
                else:
                    self.assertIn(expected, line)

    def test_855_shift_assets_are_per_shift_and_context_only_assets_are_excluded(self) -> None:
        base = self._830_single_demand_projection(
            name="Clément",
            phone="555-0151",
            two_shifts=True,
        )
        project_row = base.projects[0]
        day = project_row.days[0]
        task = day.tasks[0]
        first, second = task.request_assignments
        mat151 = replace(
            first.assets[0],
            allocation_id="ALLOC-151",
            asset_id="ASSET-151",
            asset_code="MAT151",
            association_kind="OWNED_SHIFT",
        )
        mat204 = replace(
            first.assets[0],
            allocation_id="ALLOC-204",
            asset_id="ASSET-204",
            asset_code="MAT204",
            association_kind="INHERITED_RESOURCE_PERIOD",
        )
        context = replace(
            first.assets[0],
            allocation_id="ALLOC-CONTEXT",
            asset_id="ASSET-CONTEXT",
            asset_code="CTX999",
            association_kind="RELATED_REQUEST",
        )
        changed_task = replace(
            task,
            request_assignments=(
                replace(first, assets=(mat151, context)),
                replace(second, assets=(mat204,)),
            ),
        )
        source = replace(
            base,
            projects=(
                replace(
                    project_row,
                    days=(replace(day, tasks=(changed_task,)),),
                ),
            ),
        )
        body = build_project_confirmation_batch(source).drafts[0].body
        self.assertIn("• SHAWN FRAPPIER-BARABÉ — 3 h (MAT151)", body)
        self.assertIn("• SHAWN FRAPPIER-BARABÉ — 5 h (MAT204)", body)
        self.assertNotIn("CTX999", body)
        self.assertNotIn("Actif", body)
        self.assertNotIn("()", body)

    def test_855_dynamic_description_contact_and_employee_values_are_html_escaped(self) -> None:
        base = self._830_single_demand_projection(
            name="Clément <Chef> & équipe",
            phone="555-0151",
        )
        project_row = base.projects[0]
        day = project_row.days[0]
        task = day.tasks[0]
        changed_task = replace(
            task,
            task_description="Installer <harnais> & tester",
            request_assignments=(
                replace(
                    task.request_assignments[0],
                    resource_name="654 - DAVID <DERY> & FILS",
                ),
            ),
        )
        source = replace(
            base,
            projects=(
                replace(
                    project_row,
                    project_name="Rheinmetal <Nord> & Sud",
                    days=(replace(day, tasks=(changed_task,)),),
                ),
            ),
        )
        draft = build_project_confirmation_batch(source).drafts[0]
        html = draft.body_html or ""
        self.assertIn("Rheinmetal &lt;Nord&gt; &amp; Sud", html)
        self.assertIn("Installer &lt;harnais&gt; &amp; tester", html)
        self.assertIn("Clément &lt;Chef&gt; &amp; équipe", html)
        self.assertIn("DAVID &lt;DERY&gt; &amp; FILS", html)
        self.assertNotIn("<Chef>", html)
        self.assertNotIn("<harnais>", html)

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
        self.assertIn("• Alice", body)
        self.assertNotIn("Alice — 8 h", body)
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
        self.assertNotIn("Actif :", body)
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

    def test_882_canonical_recipients_deduplicate_by_email_not_display_name(self) -> None:
        to_first = participant(
            "Même nom",
            contact_id="C-TO-1",
            user_id="U-TO-1",
            address="Person" + chr(64) + "Example.test",
        )
        to_duplicate = participant(
            "Autre rôle",
            contact_id="C-TO-2",
            user_id="U-TO-2",
            address="person" + chr(64) + "example.TEST",
        )
        to_same_name_distinct = participant(
            "Même nom",
            contact_id="C-TO-3",
            user_id="U-TO-3",
            address="other" + chr(64) + "example.test",
        )
        cc_first = participant(
            "CC premier",
            contact_id="C-CC-1",
            user_id="U-CC-1",
            address="CcShared" + chr(64) + "Example.test",
        )
        cc_duplicate = participant(
            "CC autre rôle",
            contact_id="C-CC-2",
            user_id="U-CC-2",
            address="ccshared" + chr(64) + "example.TEST",
        )
        cc_conflicts_with_to = participant(
            "Ressource avec adresse To",
            contact_id="C-CC-3",
            user_id="U-CC-3",
            address="PERSON" + chr(64) + "EXAMPLE.TEST",
        )

        to_recipients, cc_recipients = canonical_project_recipients(
            (to_first, to_duplicate, to_same_name_distinct),
            (cc_first, cc_duplicate, cc_conflicts_with_to),
        )

        self.assertEqual(to_recipients, (to_first, to_same_name_distinct))
        self.assertEqual(cc_recipients, (cc_first,))
        self.assertEqual(to_recipients[0].display_name, "Même nom")
        self.assertEqual(to_recipients[1].display_name, "Même nom")

    def test_882_duplicate_role_email_does_not_remove_projection_provenance(self) -> None:
        requester = participant(
            "Demandeuse distincte",
            contact_id="C-REQ-882",
            user_id="U-REQ-882",
            address="PM" + chr(64) + "INVALID.TEST",
        )
        source = projection(project(requesters=(requester,)))
        draft = build_project_confirmation_batch(source).drafts[0]

        self.assertEqual(
            [row.email for row in draft.to_recipients],
            [email("pm")],
        )
        self.assertEqual(source.projects[0].requesters, (requester,))
        restored = deserialize_project_projection(
            serialize_project_projection(source)
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

    def test_871_manager_employee_prefix_is_presentation_only(self) -> None:
        for raw_name in (
            "12345 - Jean Tremblay",
            "12345 – Jean Tremblay",
            "12345 — Jean Tremblay",
        ):
            with self.subTest(raw_name=raw_name):
                source = project()
                source = replace(
                    source,
                    project_manager=replace(
                        source.project_manager,
                        display_name=raw_name,
                    ),
                )
                draft = build_project_confirmation_batch(
                    projection(source)
                ).drafts[0]
                self.assertIn("Responsable projet\nJean Tremblay", draft.body)
                self.assertNotIn(raw_name, draft.body)
                self.assertIn(">Jean Tremblay</div>", draft.body_html or "")
                self.assertNotIn(raw_name, draft.body_html or "")
                self.assertEqual(
                    draft.to_recipients[0].display_name,
                    raw_name,
                )

        unchanged = "Jean 12345 Tremblay"
        source = project()
        source = replace(
            source,
            project_manager=replace(
                source.project_manager,
                display_name=unchanged,
            ),
        )
        draft = build_project_confirmation_batch(projection(source)).drafts[0]
        self.assertIn(f"Responsable projet\n{unchanged}", draft.body)
        self.assertIn(unchanged, draft.body_html or "")

    def test_body_groups_multiple_days_tasks_and_includes_responsible_phone(self) -> None:
        draft = build_project_confirmation_batch(
            projection(project(second_day=True))
        ).drafts[0]

        self.assertIn("1000 - Projet 1000", draft.body)
        self.assertIn("Responsable projet\nChargé Démo", draft.body)
        self.assertIn(
            "Responsable opérationnel : Jean Responsable — " + "555" + "-" + "0100",
            draft.body,
        )
        self.assertIn("210 Installation de poteaux", draft.body)
        self.assertIn("220 Mise en service", draft.body)
        self.assertIn("Lundi le 21 septembre 2026", draft.body)
        self.assertIn("Mardi le 22 septembre 2026", draft.body)
        self.assertIn("• Alice", draft.body)
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
            "• Alice — Responsable : Responsable Alice",
            draft.body,
        )
        self.assertIn(
            "• Bob — Responsable : Responsable Bob",
            draft.body,
        )
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

    def test_855_p2_binds_shift_hours_to_responsibility_semantics(self) -> None:
        employee = replace(
            resource("R1", "Alice", address=email("alice"), hours=8.0),
            shift_ids=("S-A", "S-B"),
        )
        responsible_a = responsible("Responsable A", contact_id="C-A")
        responsible_b = responsible("Responsable B", contact_id="C-B")
        base_project = project(resources=(employee,))
        base_task = base_project.days[0].tasks[0]

        def snapshot(
            *,
            first_hours: float,
            second_hours: float,
            shift_ids: tuple[str, str] = ("S-A", "S-B"),
            version: int = 2,
        ) -> ProjectCommunicationProjection:
            first_shift, second_shift = shift_ids
            task = replace(
                base_task,
                resources=(replace(employee, shift_ids=shift_ids),),
                operational_responsibles=(responsible_a, responsible_b),
                responsibility_assignments=(
                    ProjectCommunicationResponsibilityAssignment(
                        shift_id=first_shift,
                        resource_id="R1",
                        resource_name="Alice",
                        operational_responsible=responsible_a,
                    ),
                    ProjectCommunicationResponsibilityAssignment(
                        shift_id=second_shift,
                        resource_id="R1",
                        resource_name="Alice",
                        operational_responsible=responsible_b,
                    ),
                ),
                request_assignments=(
                    ProjectCommunicationRequestAssignment(
                        shift_id=first_shift,
                        requirement_id="REQ-A",
                        workforce_request_id="D1",
                        request_number="DMO-1",
                        resource_id="R1",
                        resource_name="Alice",
                        hours=first_hours,
                        site_contact_name=None,
                        site_contact_phone=None,
                        assets=(),
                        standard_hours=8.0 if version == 2 else None,
                    ),
                    ProjectCommunicationRequestAssignment(
                        shift_id=second_shift,
                        requirement_id="REQ-B",
                        workforce_request_id="D1",
                        request_number="DMO-1",
                        resource_id="R1",
                        resource_name="Alice",
                        hours=second_hours,
                        site_contact_name=None,
                        site_contact_phone=None,
                        assets=(),
                        standard_hours=8.0 if version == 2 else None,
                    ),
                ),
            )
            return ProjectCommunicationProjection(
                week_start=WEEK,
                week_end=date(2026, 9, 27),
                projects=(
                    replace(
                        base_project,
                        days=(
                            ProjectCommunicationDay(
                                day=WEEK,
                                tasks=(task,),
                            ),
                        ),
                    ),
                ),
                request_provenance_version=version,
                request_provenance_complete=True,
            )

        before = snapshot(first_hours=3.0, second_hours=5.0)
        after = snapshot(first_hours=5.0, second_hours=3.0)

        self.assertNotEqual(
            project_projection_fingerprint(before),
            project_projection_fingerprint(after),
        )
        changes = compare_project_communication_projections(before, after)
        self.assertEqual(
            [(row.project_id, row.kind) for row in changes],
            [("P1", "modified")],
        )
        before_batch = build_project_confirmation_batch(before)
        after_batch = build_project_confirmation_batch(after)
        self.assertNotEqual(
            before_batch.drafts[0].body,
            after_batch.drafts[0].body,
        )
        self.assertNotEqual(
            before_batch.drafts[0].body_html,
            after_batch.drafts[0].body_html,
        )
        self.assertEqual(
            before_batch.drafts[0].to_recipients,
            after_batch.drafts[0].to_recipients,
        )
        self.assertEqual(
            before_batch.drafts[0].cc_recipients,
            after_batch.drafts[0].cc_recipients,
        )

        rebuilt = snapshot(
            first_hours=3.0,
            second_hours=5.0,
            shift_ids=("S-REBUILT-A", "S-REBUILT-B"),
        )
        self.assertEqual(
            project_projection_fingerprint(before),
            project_projection_fingerprint(rebuilt),
        )
        self.assertEqual(
            compare_project_communication_projections(before, rebuilt),
            (),
        )

        before_v1 = snapshot(
            first_hours=3.0,
            second_hours=5.0,
            version=1,
        )
        after_v1 = snapshot(
            first_hours=5.0,
            second_hours=3.0,
            version=1,
        )
        self.assertEqual(
            project_projection_fingerprint(before_v1),
            project_projection_fingerprint(after_v1),
        )
        self.assertEqual(
            compare_project_communication_projections(before_v1, after_v1),
            (),
        )

    def test_855_responsibility_redundancy_requires_canonical_manager_provenance(self) -> None:
        manager_fallback = ContactResolution(
            status=STATUS_RESOLVED,
            contact_id="C-PM",
            display_name="Chargé Démo",
            email=None,
            phone=None,
            source_type=SOURCE_PROJECT_MANAGER,
            source_entity_id="P1",
        )
        fallback = build_project_confirmation_batch(
            projection(
                project(
                    responsible_contact=manager_fallback,
                )
            )
        ).drafts[0]
        self.assertNotIn("Responsable opérationnel :", fallback.body)

        same_name_distinct_identity = ContactResolution(
            status=STATUS_RESOLVED,
            contact_id="C-DISTINCT",
            display_name="Chargé Démo",
            email=None,
            phone=None,
            source_type="TASK_RESPONSIBLE",
            source_entity_id="TASK-1",
        )
        distinct = build_project_confirmation_batch(
            projection(
                project(
                    responsible_contact=same_name_distinct_identity,
                )
            )
        ).drafts[0]
        expected = "Responsable opérationnel : Chargé Démo"
        self.assertIn(expected, distinct.body)
        self.assertIsNotNone(distinct.body_html)
        self.assertIn(expected, distinct.body_html or "")

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

        self.assertIn("• Alice", draft.body)
        self.assertNotIn("Tentative", draft.body)
        self.assertNotIn("hors horaire", draft.body)

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

        self.assertEqual(first.drafts[0].body, second.drafts[0].body)
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
            replace(
                item,
                workforce_request_id="DEMAND-124",
                request_number="DMO-2026-0124",
                site_contact_name=name,
                site_contact_phone=phone,
                assets=(asset,),
                standard_hours=8.0,
            )
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
            request_provenance_version=2,
            request_provenance_complete=True,
        )

    def test_866_single_demand_without_contact_stays_compact(self) -> None:
        current = self._830_single_demand_projection(name=None, phone=None)
        batch = build_project_confirmation_batch(current)
        body = batch.drafts[0].body
        self.assertIn("Contact chantier\nAucun contact chantier renseigné", body)
        self.assertIn("• SHAWN FRAPPIER-BARABÉ (MAT162)", body)
        self.assertEqual(body.count("• SHAWN FRAPPIER-BARABÉ"), 1)
        self.assertNotIn("654 -", body)
        self.assertNotIn("Actif", body)
        self.assertNotIn("— 8 h", body)

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
                expected_contact = expected.replace(" — ", " : ")
                self.assertIn(
                    "Contact chantier\n" + expected_contact,
                    draft.body,
                )
                self.assertEqual(
                    draft.body.count("• SHAWN FRAPPIER-BARABÉ"), 1
                )
                self.assertIn("• SHAWN FRAPPIER-BARABÉ (MAT162)", draft.body)
                self.assertNotIn("654 -", draft.body)
                self.assertNotIn("Actif", draft.body)
                self.assertNotIn("— 8 h", draft.body)

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
        self.assertEqual(draft.body.count("• SHAWN FRAPPIER-BARABÉ"), 2)
        self.assertIn("• SHAWN FRAPPIER-BARABÉ — 3 h (MAT162)", draft.body)
        self.assertIn("• SHAWN FRAPPIER-BARABÉ — 5 h (MAT162)", draft.body)
        self.assertNotIn("654 -", draft.body)
        self.assertNotIn("Actif", draft.body)

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
            "Demande DMO-2026-0124 — Bob Barker : 5149370707",
            body,
        )
        self.assertIn(
            "Sans demande — Aucun contact chantier renseigné",
            body,
        )
        self.assertIn(
            "Demande DMO-2026-0124 — 210 Installation de poteaux\n"
            "• SHAWN FRAPPIER-BARABÉ — 4 h (MAT162)",
            body,
        )
        self.assertIn(
            "Sans demande — 210 Installation de poteaux\n"
            "• SHAWN FRAPPIER-BARABÉ — 4 h (MAT162)",
            body,
        )
        self.assertEqual(body.count("• SHAWN FRAPPIER-BARABÉ"), 2)
        self.assertNotIn("654 -", body)
        self.assertNotIn("Actif", body)

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
        self.assertIn("Responsable projet\nChargé Démo", original_body)
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
                    "Responsable projet\nChargé Démo\n" + number,
                    body,
                )
                self.assertIn(
                    "Responsable opérationnel : Jean Responsable",
                    body,
                )
                self.assertIn("Contact chantier\n", body)
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
        self.assertIn("Chargé Démo\n" + synthetic_phone("0100"), draft.body)
        self.assertIn("Contact chantier\nChantier : 555-0300", draft.body)
        self.assertEqual(draft.body.count("• Alice"), 1)
        self.assertNotIn("part de la demande", draft.body)
        self.assertNotIn("Totaux planifiés", draft.body)

    def test_880_v3_request_manager_phone_preserves_multi_request_provenance(self) -> None:
        base = projection(
            project(
                resources=(
                    resource("R1", "Alice", address=email("alice")),
                    resource("R2", "Bob", address=email("bob")),
                )
            )
        )
        project_row = base.projects[0]
        day = project_row.days[0]
        task = day.tasks[0]
        first, second = task.request_assignments

        def current(
            first_enabled: bool,
            first_phone: str | None,
            second_enabled: bool,
            second_phone: str | None,
        ) -> ProjectCommunicationProjection:
            v3_task = replace(
                task,
                request_assignments=(
                    replace(
                        first,
                        workforce_request_id="D1",
                        request_number="DMO-1",
                        standard_hours=8.0,
                        include_project_manager_contact_phone=first_enabled,
                        project_manager_contact_phone=first_phone,
                    ),
                    replace(
                        second,
                        workforce_request_id="D2",
                        request_number="DMO-2",
                        standard_hours=8.0,
                        include_project_manager_contact_phone=second_enabled,
                        project_manager_contact_phone=second_phone,
                    ),
                ),
            )
            return replace(
                base,
                request_provenance_version=3,
                projects=(
                    replace(
                        project_row,
                        days=(replace(day, tasks=(v3_task,)),),
                    ),
                ),
            )

        none = current(False, None, False, None)
        none_body = build_project_confirmation_batch(none).drafts[0].body
        self.assertNotIn(synthetic_phone("0880"), none_body)
        self.assertNotIn(synthetic_phone("0881"), none_body)

        one = current(True, synthetic_phone("0880"), False, synthetic_phone("0999"))
        one_body = build_project_confirmation_batch(one).drafts[0].body
        self.assertEqual(one_body.count(synthetic_phone("0880")), 1)
        self.assertNotIn(synthetic_phone("0999"), one_body)

        same = current(
            True,
            synthetic_phone("0880"),
            True,
            "  " + synthetic_phone("0880") + "  ",
        )
        same_body = build_project_confirmation_batch(same).drafts[0].body
        self.assertEqual(same_body.count(synthetic_phone("0880")), 1)

        different = current(
            True,
            synthetic_phone("0880"),
            True,
            synthetic_phone("0881"),
        )
        different_body = build_project_confirmation_batch(different).drafts[0].body
        self.assertIn(
            f"Demande DMO-1 — 210 Installation de poteaux — {synthetic_phone('0880')}",
            different_body,
        )
        self.assertIn(
            f"Demande DMO-2 — 210 Installation de poteaux — {synthetic_phone('0881')}",
            different_body,
        )

        one_fingerprint = project_projection_fingerprint(one)
        changed_phone = current(True, synthetic_phone("0882"), False, synthetic_phone("0999"))
        self.assertNotEqual(
            project_projection_fingerprint(changed_phone),
            one_fingerprint,
        )
        opted_out_stored_change = current(
            True,
            synthetic_phone("0880"),
            False,
            synthetic_phone("0777"),
        )
        self.assertEqual(
            project_projection_fingerprint(opted_out_stored_change),
            one_fingerprint,
        )

        encoded = serialize_project_projection(different)
        self.assertIn('"request_provenance_version":3', encoded)
        self.assertIn('"include_project_manager_contact_phone":true', encoded)
        restored = deserialize_project_projection(encoded)
        self.assertEqual(restored, different)
        self.assertEqual(
            project_projection_fingerprint(restored),
            project_projection_fingerprint(different),
        )
        opted_out_with_stored_phone = current(
            False,
            synthetic_phone("0770"),
            False,
            synthetic_phone("0771"),
        )
        opted_out_body = build_project_confirmation_batch(
            opted_out_with_stored_phone
        ).drafts[0].body
        self.assertNotIn(synthetic_phone("0770"), opted_out_body)
        self.assertNotIn(synthetic_phone("0771"), opted_out_body)
        self.assertEqual(
            deserialize_project_projection(
                serialize_project_projection(opted_out_with_stored_phone)
            ),
            opted_out_with_stored_phone,
        )

        malformed_nonboolean = json.loads(encoded)
        malformed_nonboolean["projects"][0]["days"][0]["tasks"][0][
            "request_assignments"
        ][0]["include_project_manager_contact_phone"] = "false"
        with self.assertRaisesRegex(ValueError, "non booléenne"):
            deserialize_project_projection(
                json.dumps(malformed_nonboolean, ensure_ascii=False)
            )

        malformed_legacy_erp = json.loads(
            serialize_project_projection(opted_out_with_stored_phone)
        )
        malformed_legacy_erp["projects"][0][
            "project_manager_erp_phone"
        ] = synthetic_phone("0831")
        with self.assertRaisesRegex(ValueError, "téléphone ERP"):
            deserialize_project_projection(
                json.dumps(malformed_legacy_erp, ensure_ascii=False)
            )

        directly_malformed = replace(
            different,
            projects=(
                replace(
                    different.projects[0],
                    days=(
                        replace(
                            different.projects[0].days[0],
                            tasks=(
                                replace(
                                    different.projects[0].days[0].tasks[0],
                                    request_assignments=(
                                        replace(
                                            different.projects[0]
                                            .days[0]
                                            .tasks[0]
                                            .request_assignments[0],
                                            include_project_manager_contact_phone="false",
                                        ),
                                        different.projects[0]
                                        .days[0]
                                        .tasks[0]
                                        .request_assignments[1],
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )
        with self.assertRaisesRegex(ValueError, "booléenne"):
            serialize_project_projection(directly_malformed)

    def test_880_historical_v2_snapshot_keeps_831_phone_without_v3_enrichment(self) -> None:
        base = projection(project())
        project_row = base.projects[0]
        day = project_row.days[0]
        task = day.tasks[0]
        v2_task = replace(
            task,
            request_assignments=tuple(
                replace(row, standard_hours=8.0)
                for row in task.request_assignments
            ),
        )
        historical = replace(
            base,
            request_provenance_version=2,
            projects=(
                replace(
                    project_row,
                    project_manager_erp_phone=synthetic_phone("0831"),
                    days=(replace(day, tasks=(v2_task,)),),
                ),
            ),
        )
        payload = serialize_project_projection(historical)
        self.assertNotIn("include_project_manager_contact_phone", payload)
        self.assertNotIn("project_manager_contact_phone", payload)
        restored = deserialize_project_projection(payload)
        self.assertEqual(restored.request_provenance_version, 2)
        self.assertIn(
            f"Responsable projet\nChargé Démo\n{synthetic_phone('0831')}",
            build_project_confirmation_batch(restored).drafts[0].body,
        )
        self.assertEqual(serialize_project_projection(restored), payload)

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
                standard_hours=8.0,
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
                standard_hours=8.0,
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
                request_provenance_version=2,
                request_provenance_complete=True,
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
        self.assertIn("Lundi le 21 septembre 2026", one.body)
        self.assertIn("Demande DM-A — Jean : 555-0101", one.body)
        self.assertIn("Demande DM-B — Pierre : 555-0202", one.body)
        self.assertIn(
            "Demande DM-A — 210 Installation de poteaux\n• Alice — 2 h",
            one.body,
        )
        self.assertIn(
            "Demande DM-B — 210 Installation de poteaux\n• Alice — 6 h",
            one.body,
        )
        self.assertIn(
            "Demande DM-A — 210 Installation de poteaux\n• Alice — 6 h",
            two.body,
        )
        self.assertIn(
            "Demande DM-B — 210 Installation de poteaux\n• Alice — 2 h",
            two.body,
        )
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
                    site_contact_name="Jean", site_contact_phone="555-0101",
                    standard_hours=8.0),
            replace(by_id["R2"], workforce_request_id="REQ-B", request_number="DM-B",
                    site_contact_name="Jean", site_contact_phone="555-0101",
                    standard_hours=8.0),
            replace(by_id["R3"], workforce_request_id=None, request_number=None,
                    site_contact_name=None, site_contact_phone=None,
                    standard_hours=8.0),
        )
        updated_task = replace(task, request_assignments=provenance)
        updated_day = replace(base.projects[0].days[0], tasks=(updated_task,))
        current = replace(
            base,
            projects=(replace(base.projects[0], days=(updated_day,)),),
            request_provenance_version=2,
            request_provenance_complete=True,
        )
        draft = build_project_confirmation_batch(current).drafts[0]
        self.assertIn("Demande DM-A — Jean : 555-0101", draft.body)
        self.assertIn("Demande DM-B — Jean : 555-0101", draft.body)
        self.assertIn("Sans demande — Aucun contact chantier renseigné", draft.body)
        self.assertEqual(draft.body.count("Jean : 555-0101"), 2)
        self.assertEqual(len(build_project_confirmation_batch(current).drafts), 1)
        self.assertEqual({row.email for row in draft.cc_recipients}, {email("alice"), email("bob"), email("carol")})
        self.assertIn("• Alice — 2 h", draft.body)
        self.assertIn("• Bob — 6 h", draft.body)
        self.assertIn("• Carol — 1 h", draft.body)

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

    def test_889_v4_round_trip_fingerprint_and_historical_shapes(self) -> None:
        current = v4_shift_projection(
            (
                {
                    "shift_id": "S-V4-1",
                    "resource_id": "R-SHARED",
                    "resource_name": "Mary",
                    "confirmation": "Confirmée",
                    "allocation_type": "Flexible",
                    "outside_schedule": False,
                    "task_id": "TASK-A",
                    "task_code": "210",
                },
                {
                    "shift_id": "S-V4-2",
                    "resource_id": "R-SHARED",
                    "resource_name": "Mary",
                    "confirmation": "Tentative",
                    "allocation_type": "Rigide",
                    "outside_schedule": True,
                    "task_id": "TASK-B",
                    "task_code": "220",
                },
            )
        )
        payload = serialize_project_projection(current)
        self.assertIn('"request_provenance_version":4', payload)
        for key in (
            "confirmation",
            "allocation_type",
            "outside_schedule",
            "task_id",
            "task_code",
            "task_description",
        ):
            self.assertIn(f'"{key}"', payload)
        restored = deserialize_project_projection(payload)
        self.assertEqual(restored, current)
        self.assertEqual(
            project_projection_fingerprint(restored),
            project_projection_fingerprint(current),
        )

        for version in (1, 2, 3):
            historical = historical_version(current, version)
            frozen = serialize_project_projection(historical)
            for key in (
                '"confirmation"',
                '"allocation_type"',
                '"outside_schedule"',
                '"task_id"',
                '"task_code"',
                '"task_description"',
            ):
                self.assertNotIn(key, frozen)
            self.assertEqual(
                serialize_project_projection(
                    deserialize_project_projection(frozen)
                ),
                frozen,
            )

        identity_changed = v4_shift_projection(
            (
                {
                    "shift_id": "S-V4-NEW-1",
                    "resource_id": "R-SHARED",
                    "resource_name": "Mary",
                    "confirmation": "Confirmée",
                    "allocation_type": "Flexible",
                    "outside_schedule": False,
                    "task_id": "TASK-A",
                    "task_code": "210",
                },
                {
                    "shift_id": "S-V4-NEW-2",
                    "resource_id": "R-SHARED",
                    "resource_name": "Mary",
                    "confirmation": "Tentative",
                    "allocation_type": "Rigide",
                    "outside_schedule": True,
                    "task_id": "TASK-B",
                    "task_code": "220",
                },
            )
        )
        self.assertNotEqual(
            project_projection_fingerprint(current),
            project_projection_fingerprint(identity_changed),
        )
        self.assertEqual(
            project_projection_fingerprint(historical_version(current, 3)),
            project_projection_fingerprint(historical_version(identity_changed, 3)),
        )

    def test_889_v4_raw_capability_validation_fails_closed(self) -> None:
        current = v4_shift_projection(
            (
                {
                    "shift_id": "S-V4",
                    "resource_id": "R-MARY",
                    "resource_name": "Mary",
                },
            )
        )
        encoded = json.loads(serialize_project_projection(current))
        assignment = encoded["projects"][0]["days"][0]["tasks"][0][
            "request_assignments"
        ][0]

        missing = json.loads(json.dumps(encoded))
        del missing["projects"][0]["days"][0]["tasks"][0][
            "request_assignments"
        ][0]["confirmation"]
        with self.assertRaisesRegex(ValueError, "v4 incomplet"):
            deserialize_project_projection(json.dumps(missing))

        malformed_bool = json.loads(json.dumps(encoded))
        malformed_bool["projects"][0]["days"][0]["tasks"][0][
            "request_assignments"
        ][0]["outside_schedule"] = 1
        with self.assertRaisesRegex(ValueError, "non booléen"):
            deserialize_project_projection(json.dumps(malformed_bool))

        downgraded = json.loads(json.dumps(encoded))
        downgraded["request_provenance_version"] = 3
        with self.assertRaisesRegex(ValueError, "faits Shift v4"):
            deserialize_project_projection(json.dumps(downgraded))

        boolean_version = json.loads(json.dumps(encoded))
        boolean_version["request_provenance_version"] = True
        with self.assertRaisesRegex(ValueError, "Version"):
            deserialize_project_projection(json.dumps(boolean_version))

        direct_old = historical_version(current, 3)
        direct_assignment = (
            direct_old.projects[0].days[0].tasks[0].request_assignments[0]
        )
        direct_old = replace(
            direct_old,
            projects=(
                replace(
                    direct_old.projects[0],
                    days=(
                        replace(
                            direct_old.projects[0].days[0],
                            tasks=(
                                replace(
                                    direct_old.projects[0].days[0].tasks[0],
                                    request_assignments=(
                                        replace(
                                            direct_assignment,
                                            confirmation="Confirmée",
                                        ),
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )
        with self.assertRaisesRegex(ValueError, "v4"):
            serialize_project_projection(direct_old)

    def test_889_shift_delta_only_renders_changed_rows(self) -> None:
        baseline_specs = (
            {"shift_id": "S-JOHN", "resource_id": "R-JOHN", "resource_name": "John"},
            {"shift_id": "S-MARY", "resource_id": "R-MARY", "resource_name": "Mary"},
            {"shift_id": "S-STEVE", "resource_id": "R-STEVE", "resource_name": "Steve"},
        )
        baseline = v4_shift_projection(baseline_specs)

        removed = v4_shift_projection((baseline_specs[0], baseline_specs[2]))
        removed_draft = build_project_delta_batch(baseline, removed).drafts[0]
        removed_body = removed_draft.body
        self.assertIn("Retiré — Mary", removed_body)
        self.assertNotIn("John", removed_body)
        self.assertNotIn("Steve", removed_body)
        self.assertIn("Retiré — Mary", removed_draft.body_html or "")
        self.assertNotIn("John", removed_draft.body_html or "")
        self.assertNotIn("Steve", removed_draft.body_html or "")

        replaced_specs = (
            baseline_specs[0],
            {
                "shift_id": "S-MARY",
                "resource_id": "R-JERRY",
                "resource_name": "Jerry",
            },
            baseline_specs[2],
        )
        replaced = v4_shift_projection(replaced_specs)
        replaced_draft = build_project_delta_batch(baseline, replaced).drafts[0]
        replaced_body = replaced_draft.body
        self.assertIn("Remplacé — Mary → Jerry", replaced_body)
        self.assertIn(email("r-mary"), [row.email for row in replaced_draft.cc_recipients])
        self.assertIn(email("r-jerry"), [row.email for row in replaced_draft.cc_recipients])
        self.assertNotIn("John", replaced_body)
        self.assertNotIn("Steve", replaced_body)

        distinct_specs = (
            baseline_specs[0],
            {
                "shift_id": "S-JERRY",
                "resource_id": "R-JERRY",
                "resource_name": "Jerry",
            },
            baseline_specs[2],
        )
        distinct = v4_shift_projection(distinct_specs)
        distinct_body = build_project_delta_batch(baseline, distinct).drafts[0].body
        self.assertIn("Retiré — Mary", distinct_body)
        self.assertIn("Ajouté — Jerry", distinct_body)
        self.assertNotIn("Remplacé", distinct_body)

        moved_specs = (
            baseline_specs[0],
            {
                "shift_id": "S-MARY",
                "resource_id": "R-MARY",
                "resource_name": "Mary",
                "day": date(2026, 9, 22),
            },
            baseline_specs[2],
        )
        moved = v4_shift_projection(moved_specs)
        moved_body = build_project_delta_batch(baseline, moved).drafts[0].body
        self.assertIn("Déplacé — Mary", moved_body)
        self.assertIn("Lundi le 21 septembre 2026 → Mardi le 22 septembre 2026", moved_body)

        hours_specs = (
            baseline_specs[0],
            {
                "shift_id": "S-MARY",
                "resource_id": "R-MARY",
                "resource_name": "Mary",
                "hours": 4.0,
            },
            baseline_specs[2],
        )
        hours = v4_shift_projection(hours_specs)
        hours_body = build_project_delta_batch(baseline, hours).drafts[0].body
        self.assertIn("Modifié — Mary", hours_body)
        self.assertIn("heures 8 h → 4 h", hours_body)

        confirmation_specs = (
            baseline_specs[0],
            {
                "shift_id": "S-MARY",
                "resource_id": "R-MARY",
                "resource_name": "Mary",
                "confirmation": "Tentative",
            },
            baseline_specs[2],
        )
        confirmation = v4_shift_projection(confirmation_specs)
        confirmation_body = build_project_delta_batch(
            baseline, confirmation
        ).drafts[0].body
        self.assertIn("Modifié — Mary", confirmation_body)
        self.assertIn("confirmation Confirmée → Tentative", confirmation_body)

        simultaneous_specs = (
            baseline_specs[0],
            {
                "shift_id": "S-MARY",
                "resource_id": "R-JERRY",
                "resource_name": "Jerry",
                "day": date(2026, 9, 22),
                "hours": 4.0,
                "confirmation": "Tentative",
            },
            baseline_specs[2],
        )
        simultaneous = v4_shift_projection(simultaneous_specs)
        simultaneous_body = build_project_delta_batch(
            baseline, simultaneous
        ).drafts[0].body
        self.assertIn("Remplacé — Mary → Jerry", simultaneous_body)
        self.assertIn("date Lundi le 21 septembre 2026 → Mardi le 22 septembre 2026", simultaneous_body)
        self.assertIn("heures 8 h → 4 h", simultaneous_body)
        self.assertIn("confirmation Confirmée → Tentative", simultaneous_body)

    def test_889_p4_detects_aggregate_preserving_shift_reassociation(self) -> None:
        before = v4_shift_projection(
            (
                {
                    "shift_id": "S-1",
                    "resource_id": "R-MARY",
                    "resource_name": "Mary",
                    "confirmation": "Confirmée",
                    "allocation_type": "Flexible",
                    "outside_schedule": False,
                },
                {
                    "shift_id": "S-2",
                    "resource_id": "R-MARY",
                    "resource_name": "Mary",
                    "confirmation": "Tentative",
                    "allocation_type": "Rigide",
                    "outside_schedule": True,
                },
            )
        )
        after = v4_shift_projection(
            (
                {
                    "shift_id": "S-1",
                    "resource_id": "R-MARY",
                    "resource_name": "Mary",
                    "confirmation": "Tentative",
                    "allocation_type": "Rigide",
                    "outside_schedule": True,
                },
                {
                    "shift_id": "S-2",
                    "resource_id": "R-MARY",
                    "resource_name": "Mary",
                    "confirmation": "Confirmée",
                    "allocation_type": "Flexible",
                    "outside_schedule": False,
                },
            )
        )
        self.assertNotEqual(
            project_projection_fingerprint(before),
            project_projection_fingerprint(after),
        )
        body = build_project_delta_batch(before, after).drafts[0].body
        self.assertEqual(body.count("Modifié — Mary"), 2)
        self.assertIn("confirmation Confirmée → Tentative", body)
        self.assertIn("confirmation Tentative → Confirmée", body)

    def test_889_historical_refresh_is_explicit_until_v4_baseline(self) -> None:
        current = v4_shift_projection(
            (
                {"shift_id": "S-JOHN", "resource_id": "R-JOHN", "resource_name": "John"},
                {"shift_id": "S-MARY", "resource_id": "R-MARY", "resource_name": "Mary"},
            )
        )
        for version in (1, 2, 3):
            with self.subTest(version=version):
                historical = historical_version(current, version)
                refresh = build_project_delta_batch(historical, current)
                self.assertEqual(len(refresh.drafts), 1)
                body = refresh.drafts[0].body
                self.assertIn("Référence historique incomplète", body)
                self.assertIn("état courant à reconfirmer", body)
                self.assertIn("John", body)
                self.assertIn("Mary", body)
                self.assertNotIn("confirmation None → Confirmée", body)
                self.assertNotIn("allocation None → Flexible", body)

                changed = v4_shift_projection(
                    (
                        {"shift_id": "S-JOHN", "resource_id": "R-JOHN", "resource_name": "John"},
                        {
                            "shift_id": "S-MARY",
                            "resource_id": "R-MARY",
                            "resource_name": "Mary",
                            "hours": 4.0,
                        },
                    )
                )
                changed_refresh = build_project_delta_batch(
                    historical,
                    changed,
                ).drafts[0].body
                self.assertIn("heures 8 h → 4 h", changed_refresh)
                self.assertIn("Référence historique incomplète", changed_refresh)

        complete = build_project_delta_batch(current, current)
        self.assertEqual(complete.drafts, ())

    def test_889_pre_provenance_baseline_uses_explicit_whole_project_refresh(self) -> None:
        current = v4_shift_projection(
            (
                {
                    "shift_id": "S-MARY",
                    "resource_id": "R-MARY",
                    "resource_name": "Mary",
                },
            )
        )
        current_project = current.projects[0]
        legacy_task = replace(
            current_project.days[0].tasks[0],
            request_assignments=(),
        )
        legacy = replace(
            current,
            projects=(
                replace(
                    current_project,
                    days=(
                        replace(
                            current_project.days[0],
                            tasks=(legacy_task,),
                        ),
                    ),
                ),
            ),
            request_provenance_version=None,
            request_provenance_complete=False,
        )
        delta = build_project_delta_batch(legacy, current)
        self.assertEqual(len(delta.drafts), 1)
        body = delta.drafts[0].body
        self.assertIn(
            "Référence historique incomplète — actualisation du projet complet.",
            body,
        )
        self.assertIn("Mary", body)
        self.assertNotIn("Modifié — Mary", body)

    def test_889_cross_project_move_is_isolated_remove_add(self) -> None:
        old_project = v4_shift_projection(
            (
                {
                    "shift_id": "S-MOVE",
                    "resource_id": "R-MARY",
                    "resource_name": "Mary",
                },
            ),
            project_id="P-A",
            project_number="1000",
            manager_local="pm-a",
        )
        new_project = v4_shift_projection(
            (
                {
                    "shift_id": "S-MOVE",
                    "resource_id": "R-MARY",
                    "resource_name": "Mary",
                },
            ),
            project_id="P-B",
            project_number="2000",
            manager_local="pm-b",
        )
        before = old_project
        after = replace(
            new_project,
            week_start=before.week_start,
            week_end=before.week_end,
        )
        delta = build_project_delta_batch(before, after)
        self.assertEqual(
            {draft.message_key for draft in delta.drafts},
            {"project:P-A", "project:P-B"},
        )
        by_key = {draft.message_key: draft for draft in delta.drafts}
        self.assertIn("Retiré — Mary", by_key["project:P-A"].body)
        self.assertIn("Ajouté — Mary", by_key["project:P-B"].body)
        self.assertEqual(
            [row.email for row in by_key["project:P-A"].to_recipients],
            [email("pm-a")],
        )
        self.assertEqual(
            [row.email for row in by_key["project:P-B"].to_recipients],
            [email("pm-b")],
        )

    def test_889_project_only_change_does_not_mark_shifts_modified(self) -> None:
        before = v4_shift_projection(
            (
                {
                    "shift_id": "S-MARY",
                    "resource_id": "R-MARY",
                    "resource_name": "Mary",
                },
            )
        )
        after = replace(
            before,
            projects=(
                replace(before.projects[0], project_name="Projet renommé"),
            ),
        )
        delta = build_project_delta_batch(before, after)
        self.assertEqual(len(delta.drafts), 1)
        body = delta.drafts[0].body
        self.assertIn("Informations du projet mises à jour.", body)
        self.assertNotIn("Modifié — Mary", body)
        self.assertNotIn("Ajouté — Mary", body)
        self.assertNotIn("Retiré — Mary", body)

    def test_889_delta_to_recipients_use_complete_before_after_project_context(self) -> None:
        before = v4_shift_projection(
            (
                {
                    "shift_id": "S-MARY",
                    "resource_id": "R-MARY",
                    "resource_name": "Mary",
                },
            ),
            manager_local="pm-old",
        )
        new_manager = participant(
            "Nouveau chargé",
            contact_id="C-PM-NEW",
            user_id="U-PM-NEW",
            address=email("pm-new"),
        )
        after = replace(
            before,
            projects=(
                replace(
                    before.projects[0],
                    project_manager=new_manager,
                ),
            ),
        )
        draft = build_project_delta_batch(before, after).drafts[0]
        self.assertEqual(
            [row.email for row in draft.to_recipients],
            [email("pm-new"), email("pm-old")],
        )
        self.assertIn("Informations du projet mises à jour.", draft.body)
        self.assertNotIn("Modifié — Mary", draft.body)

    def test_no_standalone_technician_message_is_generated(self) -> None:
        batch = build_project_confirmation_batch(projection(project()))
        self.assertTrue(batch.drafts)
        self.assertEqual({draft.audience for draft in batch.drafts}, {"project"})


if __name__ == "__main__":
    unittest.main()
