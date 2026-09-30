from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys

from sqlalchemy import delete, inspect, select
from sqlalchemy.orm import Session


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.application.security import (  # noqa: E402
    ROLE_ADMIN,
    ROLE_COORDINATOR,
    ROLE_MANAGER,
    ROLE_PROJECT_MANAGER,
    ROLE_TECHNICIAN,
)
from app.infrastructure.sql import (  # noqa: E402
    Competency,
    Project,
    RequestLine,
    RequestLineCompetency,
    Resource,
    ResourceAvailabilityRule,
    ResourceCompetency,
    ResourceRequirement,
    ResourceRequirementCompetency,
    Shift,
    SqlUserIdentityRepository,
    WorkforceRequest,
    WorkforceRequestHistory,
    WorkforceRequestPeriod,
    WorkforceRequestPeriodRequirement,
    WorkforceRequestPeriodSelection,
    WorkPackage,
    create_session_factory,
    create_sql_engine,
)


DEFAULT_DATABASE_URL = "sqlite:///./resourceplanner_server.db"
DEMO_PROJECT_IDS = ("DEMO-P-1001", "DEMO-P-1002", "DEMO-P-1003")
DEMO_RESOURCE_IDS = (
    "DEMO-R-AUTO-1",
    "DEMO-R-AUTO-2",
    "DEMO-R-INSTALL-1",
    "DEMO-R-PANEL-1",
    "DEMO-R-DRAW-1",
    "DEMO-R-PM-1",
)
DEV_IDENTITY_ISSUER = "urn:resourceplanner:dev"
DEV_RESOURCE_EXTERNAL_IDS = {
    "DEMO-R-AUTO-1": "DEV-TECH-A",
    "DEMO-R-AUTO-2": "DEV-TECH-B",
    "DEMO-R-PM-1": "DEV-PM-TECH",
}


@dataclass(frozen=True)
class DemoSeedSummary:
    week_start: date
    projects: int
    resources: int
    work_packages: int
    demands: int
    requirements: int
    shifts: int
    periods: int
    users: int


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _clear_demo_project_data(session: Session) -> None:
    request_ids = tuple(
        session.scalars(
            select(WorkforceRequest.id).where(
                WorkforceRequest.project_id.in_(DEMO_PROJECT_IDS)
            )
        ).all()
    )
    requirement_ids = tuple(
        session.scalars(
            select(ResourceRequirement.id).where(
                ResourceRequirement.project_id.in_(DEMO_PROJECT_IDS)
            )
        ).all()
    )

    if requirement_ids:
        session.execute(
            delete(Shift).where(Shift.resource_requirement_id.in_(requirement_ids))
        )
        session.execute(
            delete(WorkforceRequestPeriodRequirement).where(
                WorkforceRequestPeriodRequirement.resource_requirement_id.in_(
                    requirement_ids
                )
            )
        )
        session.execute(
            delete(ResourceRequirementCompetency).where(
                ResourceRequirementCompetency.resource_requirement_id.in_(
                    requirement_ids
                )
            )
        )

    if request_ids:
        period_ids = tuple(
            session.scalars(
                select(WorkforceRequestPeriod.id).where(
                    WorkforceRequestPeriod.workforce_request_id.in_(request_ids)
                )
            ).all()
        )
        session.execute(
            delete(WorkforceRequestPeriodSelection).where(
                WorkforceRequestPeriodSelection.workforce_request_id.in_(request_ids)
            )
        )
        if period_ids:
            session.execute(
                delete(WorkforceRequestPeriodRequirement).where(
                    WorkforceRequestPeriodRequirement.period_id.in_(period_ids)
                )
            )
        session.execute(
            delete(WorkforceRequestPeriod).where(
                WorkforceRequestPeriod.workforce_request_id.in_(request_ids)
            )
        )
        session.execute(
            delete(WorkforceRequestHistory).where(
                WorkforceRequestHistory.workforce_request_id.in_(request_ids)
            )
        )

    if requirement_ids:
        session.execute(
            delete(ResourceRequirement).where(ResourceRequirement.id.in_(requirement_ids))
        )
    if request_ids:
        session.execute(
            delete(RequestLineCompetency).where(
                RequestLineCompetency.request_line_id.in_(request_ids)
            )
        )
        session.execute(
            delete(RequestLine).where(RequestLine.workforce_request_id.in_(request_ids))
        )
        session.execute(
            delete(WorkforceRequest).where(WorkforceRequest.id.in_(request_ids))
        )

    session.execute(
        delete(WorkPackage).where(WorkPackage.project_id.in_(DEMO_PROJECT_IDS))
    )
    session.execute(delete(Project).where(Project.id.in_(DEMO_PROJECT_IDS)))


def _upsert_demo_competencies(session: Session) -> dict[str, str]:
    names = (
        "PLC",
        "SCADA",
        "MES",
        "Installation",
        "Mise en service",
        "Montage",
        "Câblage",
        "Électrique",
        "DAO",
        "Coordination",
        "Planification",
    )
    result: dict[str, str] = {}
    for index, name in enumerate(names, start=1):
        row = session.scalar(select(Competency).where(Competency.name == name))
        if row is None:
            row = Competency(
                id=f"DEMO-COMP-{index:02d}",
                name=name,
                description=f"Compétence de démonstration — {name}",
                active=True,
                sort_order=index * 10,
            )
            session.add(row)
            session.flush()
        else:
            row.active = True
        result[name] = row.id
    return result


def _upsert_demo_resources(
    session: Session,
    monday: date,
    competency_ids: dict[str, str],
) -> None:
    definitions = (
        (
            "DEMO-R-AUTO-1",
            "Démo - Automatisation 1",
            "Programmation",
            "PLC; SCADA; MES",
            10,
        ),
        (
            "DEMO-R-AUTO-2",
            "Démo - Automatisation 2",
            "Programmation",
            "PLC; SCADA",
            20,
        ),
        (
            "DEMO-R-INSTALL-1",
            "Démo - Installation 1",
            "Installation",
            "Installation; Mise en service",
            30,
        ),
        (
            "DEMO-R-PANEL-1",
            "Démo - Panneau 1",
            "Monteur de panneau",
            "Montage; Câblage",
            40,
        ),
        (
            "DEMO-R-DRAW-1",
            "Démo - Dessin 1",
            "Dessinateur",
            "Électrique; DAO",
            50,
        ),
        (
            "DEMO-R-PM-1",
            "Démo - Projet 1",
            "Gestion de projet",
            "Coordination; Planification",
            60,
        ),
    )

    for resource_id, name, resource_class, competencies, sort_order in definitions:
        resource = session.get(Resource, resource_id)
        if resource is None:
            resource = Resource(id=resource_id, name=name)
            session.add(resource)
        resource.name = name
        resource.resource_class = resource_class
        resource.competencies = competencies
        if resource_id in DEV_RESOURCE_EXTERNAL_IDS:
            resource.external_id = DEV_RESOURCE_EXTERNAL_IDS[resource_id]
        resource.note = "Ressource de démonstration locale"
        resource.active = True
        resource.sort_order = sort_order

    session.flush()

    session.execute(
        delete(ResourceCompetency).where(
            ResourceCompetency.resource_id.in_(DEMO_RESOURCE_IDS)
        )
    )
    for resource_id, _name, _resource_class, competencies, _sort_order in definitions:
        for name in [part.strip() for part in competencies.split(";") if part.strip()]:
            competency_id = competency_ids.get(name)
            if competency_id:
                session.add(
                    ResourceCompetency(
                        resource_id=resource_id,
                        competency_id=competency_id,
                    )
                )

    schedule_ids = tuple(f"DEMO-SCH-{resource_id}" for resource_id in DEMO_RESOURCE_IDS)
    session.execute(
        delete(ResourceAvailabilityRule).where(
            ResourceAvailabilityRule.id.in_(schedule_ids)
        )
    )
    schedule_start = monday - timedelta(days=180)
    schedule_end = monday + timedelta(days=365)
    for resource_id in DEMO_RESOURCE_IDS:
        session.add(
            ResourceAvailabilityRule(
                id=f"DEMO-SCH-{resource_id}",
                resource_id=resource_id,
                availability_type="Horaire standard",
                start_date=schedule_start,
                end_date=schedule_end,
                weekdays="Lun,Mar,Mer,Jeu,Ven",
                start_time=time(7, 0),
                end_time=time(15, 30),
                note="Horaire démo local",
                active=True,
            )
        )


def _upsert_dev_users(session: Session) -> tuple[int, dict[str, str]]:
    repository = SqlUserIdentityRepository(session)
    definitions = (
        ("admin", "Administrateur Démo", (ROLE_ADMIN,), None),
        ("coordinator", "Coordonnateur Démo", (ROLE_COORDINATOR,), None),
        (
            "project-manager",
            "Chargé de projet Démo",
            (ROLE_PROJECT_MANAGER,),
            "DEV-PM-A",
        ),
        ("manager", "Gestionnaire Démo", (ROLE_MANAGER,), None),
        ("technician-a", "Technicien Démo A", (ROLE_TECHNICIAN,), "DEV-TECH-A"),
        ("technician-b", "Technicien Démo B", (ROLE_TECHNICIAN,), "DEV-TECH-B"),
        (
            "project-technician",
            "Chargé de projet / Technicien Démo",
            (ROLE_PROJECT_MANAGER, ROLE_TECHNICIAN),
            "DEV-PM-TECH",
        ),
    )
    contact_ids: dict[str, str] = {}
    for subject, display_name, roles, employee_external_id in definitions:
        record = repository.upsert(
            issuer=DEV_IDENTITY_ISSUER,
            subject=subject,
            display_name=display_name,
            email=None,
            employee_external_id=employee_external_id,
            roles=roles,
            active=True,
        )
        if record.business_contact_id:
            contact_ids[subject] = record.business_contact_id
    return len(definitions), contact_ids


def seed_demo_session(session: Session, *, today: date | None = None) -> DemoSeedSummary:
    today = today or date.today()
    monday = _week_start(today)
    now = datetime.now(timezone.utc)

    _clear_demo_project_data(session)
    competency_ids = _upsert_demo_competencies(session)
    _upsert_demo_resources(session, monday, competency_ids)
    dev_user_count, dev_contact_ids = _upsert_dev_users(session)

    coordinator_contact_id = dev_contact_ids.get("coordinator")
    if coordinator_contact_id:
        for resource_id in DEMO_RESOURCE_IDS:
            resource = session.get(Resource, resource_id)
            if resource is not None:
                resource.coordinator_contact_id = coordinator_contact_id

    projects = (
        Project(
            id="DEMO-P-1001",
            number="DEMO-1001",
            name="Modernisation traitement d'eau",
            client="Client Démo A",
            project_manager_external_id="DEV-PM-A",
            project_manager_name="Chargé de projet Démo",
            project_manager_contact_id=dev_contact_ids.get("project-manager"),
            status="Actif",
        ),
        Project(
            id="DEMO-P-1002",
            number="DEMO-1002",
            name="Ligne d'emballage",
            client="Client Démo B",
            project_manager_external_id="DEV-PM-TECH",
            project_manager_name="Chargé de projet / Technicien Démo",
            project_manager_contact_id=dev_contact_ids.get("project-technician"),
            status="Actif",
        ),
        Project(
            id="DEMO-P-1003",
            number="DEMO-1003",
            name="Architecture contrôle et MES",
            client="Client Démo A",
            project_manager_external_id="DEV-PM-A",
            project_manager_name="Chargé de projet Démo",
            project_manager_contact_id=dev_contact_ids.get("project-manager"),
            status="Actif",
        ),
    )
    session.add_all(projects)

    work_packages = (
        WorkPackage(
            id="DEMO-WP-1001-01",
            project_id="DEMO-P-1001",
            code="AUT-01",
            name="Programmation PLC / SCADA",
            description="Développement et essais fonctionnels",
            start_date=monday - timedelta(days=7),
            end_date=monday + timedelta(days=18),
            planned_hours=Decimal("80"),
            status="planned",
            legacy_effort_id="DEMO-EFF-1001-01",
        ),
        WorkPackage(
            id="DEMO-WP-1001-02",
            project_id="DEMO-P-1001",
            code="MES-01",
            name="Mise en service",
            description="Essais terrain et accompagnement démarrage",
            start_date=monday + timedelta(days=7),
            end_date=monday + timedelta(days=18),
            planned_hours=Decimal("48"),
            status="planned",
            legacy_effort_id="DEMO-EFF-1001-02",
        ),
        WorkPackage(
            id="DEMO-WP-1002-01",
            project_id="DEMO-P-1002",
            code="INT-01",
            name="Intégration ligne",
            description="Installation et intégration contrôle",
            start_date=monday,
            end_date=monday + timedelta(days=11),
            planned_hours=Decimal("64"),
            status="planned",
            legacy_effort_id="DEMO-EFF-1002-01",
        ),
        WorkPackage(
            id="DEMO-WP-1003-01",
            project_id="DEMO-P-1003",
            code="ARCH-01",
            name="Architecture MES",
            description="Architecture, interfaces et modèle de données",
            start_date=monday + timedelta(days=7),
            end_date=monday + timedelta(days=25),
            planned_hours=Decimal("40"),
            status="planned",
            legacy_effort_id="DEMO-EFF-1003-01",
        ),
    )
    session.add_all(work_packages)

    requests = (
        WorkforceRequest(
            id="DEMO-DMO-001",
            legacy_demand_number="DEMO-DMO-001",
            project_id="DEMO-P-1001",
            work_package_id="DEMO-WP-1001-01",
            requester_name="Demandeur Démo",
            request_type="Projet",
            priority="Haute",
            confirmation="Confirmée",
            desired_start=monday,
            desired_end=monday + timedelta(days=2),
            description="Programmation et essais PLC / SCADA",
            resource_count=1,
            required_competencies="PLC; SCADA",
            estimated_hours=Decimal("24"),
            estimated_days=Decimal("3"),
            proposed_resource_id="DEMO-R-AUTO-1",
            status="En planification",
            approved_by_name="Coordonnateur Démo",
            approved_at=now,
            approval_comment="Jeu de données local",
        ),
        WorkforceRequest(
            id="DEMO-DMO-002",
            legacy_demand_number="DEMO-DMO-002",
            project_id="DEMO-P-1001",
            work_package_id="DEMO-WP-1001-02",
            requester_name="Demandeur Démo",
            request_type="Projet",
            priority="Normale",
            confirmation="Tentative",
            desired_start=monday + timedelta(days=2),
            desired_end=monday + timedelta(days=4),
            description="Fenêtres possibles de mise en service",
            resource_count=1,
            required_competencies="SCADA; Mise en service",
            estimated_hours=Decimal("12"),
            estimated_days=Decimal("2"),
            proposed_resource_id="DEMO-R-AUTO-2",
            status="Soumise",
        ),
        WorkforceRequest(
            id="DEMO-DMO-003",
            legacy_demand_number="DEMO-DMO-003",
            project_id="DEMO-P-1002",
            work_package_id="DEMO-WP-1002-01",
            requester_name="Demandeur Démo",
            request_type="Projet",
            priority="Normale",
            confirmation="Confirmée",
            desired_start=monday + timedelta(days=7),
            desired_end=monday + timedelta(days=11),
            description="Intégration de la nouvelle ligne",
            resource_count=2,
            required_competencies="Installation; PLC",
            estimated_hours=Decimal("40"),
            estimated_days=Decimal("5"),
            status="Brouillon",
        ),
        WorkforceRequest(
            id="DEMO-DMO-004",
            legacy_demand_number="DEMO-DMO-004",
            project_id="DEMO-P-1003",
            work_package_id="DEMO-WP-1003-01",
            requester_name="Demandeur Démo",
            request_type="Projet",
            priority="Basse",
            confirmation="Tentative",
            desired_start=monday + timedelta(days=4),
            desired_end=monday + timedelta(days=11),
            description="Préparation architecture MES à corriger",
            resource_count=1,
            required_competencies="MES; Architecture",
            estimated_hours=Decimal("16"),
            estimated_days=Decimal("2"),
            proposed_resource_id="DEMO-R-AUTO-2",
            status="À corriger",
        ),
    )
    session.add_all(requests)
    session.flush()
    session.add_all(
        [
            RequestLine(
                id=request.id,
                workforce_request_id=request.id,
                position=0,
                kind="WORKFORCE",
                slot_count=max(int(request.resource_count or 1), 1),
                required_competencies_snapshot=request.required_competencies,
                desired_start=request.desired_start,
                desired_end=request.desired_end,
                desired_active_days=request.estimated_days,
                estimated_hours=request.estimated_hours,
                confirmation=request.confirmation,
                work_package_id=request.work_package_id,
                proposed_resource_id=request.proposed_resource_id,
                description=request.description,
                active=True,
            )
            for request in requests
        ]
    )
    session.flush()

    history = (
        ("DEMO-DMO-001", "Approbation", "En planification", "Demande approuvée"),
        ("DEMO-DMO-002", "Soumission", "Soumise", "Demande soumise"),
        ("DEMO-DMO-003", "Création", "Brouillon", "Brouillon créé"),
        ("DEMO-DMO-004", "Correction", "À corriger", "Préciser l'interface MES"),
    )
    for request_id, action, status, comment in history:
        session.add(
            WorkforceRequestHistory(
                id=f"DEMO-HIST-{request_id}",
                workforce_request_id=request_id,
                action=action,
                status=status,
                comment=comment,
                actor_name="Coordonnateur Démo",
                occurred_at=now,
            )
        )

    periods = (
        WorkforceRequestPeriod(
            id="DEMO-PER-002-CUM",
            period_key="DEMO-PER-002-CUM",
            workforce_request_id="DEMO-DMO-002",
            request_line_id="DEMO-DMO-002",
            sequence=10,
            kind="CUMULATIVE",
            alternative_group=None,
            start_date=monday + timedelta(days=2),
            end_date=monday + timedelta(days=2),
            hours=Decimal("4"),
            confirmation="Tentative",
            proposed_resource_id="DEMO-R-AUTO-2",
            resource_count=1,
            note="Préparation obligatoire",
            active=True,
        ),
        WorkforceRequestPeriod(
            id="DEMO-PER-002-ALT-A",
            period_key="DEMO-PER-002-ALT-A",
            workforce_request_id="DEMO-DMO-002",
            request_line_id="DEMO-DMO-002",
            sequence=20,
            kind="ALTERNATIVE",
            alternative_group="DATE-A",
            start_date=monday + timedelta(days=3),
            end_date=monday + timedelta(days=3),
            hours=Decimal("8"),
            confirmation="Tentative",
            proposed_resource_id="DEMO-R-AUTO-2",
            resource_count=1,
            note="Option jeudi",
            active=True,
        ),
        WorkforceRequestPeriod(
            id="DEMO-PER-002-ALT-B",
            period_key="DEMO-PER-002-ALT-B",
            workforce_request_id="DEMO-DMO-002",
            request_line_id="DEMO-DMO-002",
            sequence=30,
            kind="ALTERNATIVE",
            alternative_group="DATE-A",
            start_date=monday + timedelta(days=4),
            end_date=monday + timedelta(days=4),
            hours=Decimal("8"),
            confirmation="Tentative",
            proposed_resource_id="DEMO-R-AUTO-2",
            resource_count=1,
            note="Option vendredi",
            active=True,
        ),
    )
    session.add_all(periods)
    session.flush()
    session.add(
        WorkforceRequestPeriodSelection(
            workforce_request_id="DEMO-DMO-002",
            request_line_id="DEMO-DMO-002",
            alternative_group="DATE-A",
            period_id="DEMO-PER-002-ALT-A",
            selected_at=now,
            selected_by_name="Coordonnateur Démo",
        )
    )

    requirements = (
        ResourceRequirement(
            id="DEMO-REQ-001",
            legacy_segment_id="DEMO-SEG-001",
            project_id="DEMO-P-1001",
            workforce_request_id="DEMO-DMO-001",
            source_request_line_id="DEMO-DMO-001",
            assigned_resource_id="DEMO-R-AUTO-1",
            start_date=monday,
            end_date=monday + timedelta(days=2),
            planned_hours=Decimal("24"),
            status="Planifié",
            description="Programmation PLC / SCADA",
            source_effort_id="DEMO-EFF-1001-01",
            required_competency="PLC; SCADA",
            planning_type="Flexible",
            priority="Haute",
            confirmation="Confirmée",
            confirmation_overridden=False,
            origin="REQUEST",
            created_by_name="Coordonnateur Démo",
        ),
        ResourceRequirement(
            id="DEMO-REQ-QS-001",
            legacy_segment_id="DEMO-SEG-QS-001",
            project_id="DEMO-P-1002",
            workforce_request_id=None,
            assigned_resource_id="DEMO-R-INSTALL-1",
            start_date=monday + timedelta(days=5),
            end_date=monday + timedelta(days=5),
            planned_hours=Decimal("6"),
            status="Planifié",
            description="Intervention urgente samedi",
            required_competency="Installation",
            planning_type="Fixe",
            priority="Haute",
            outside_standard_hours_allowed=True,
            confirmation="Confirmée",
            confirmation_overridden=False,
            origin="QUICK_SHIFT",
            created_by_name="Coordonnateur Démo",
        ),
        ResourceRequirement(
            id="DEMO-REQ-QS-002",
            legacy_segment_id="DEMO-SEG-QS-002",
            project_id="DEMO-P-1003",
            workforce_request_id=None,
            assigned_resource_id="DEMO-R-AUTO-2",
            start_date=monday + timedelta(days=4),
            end_date=monday + timedelta(days=4),
            planned_hours=Decimal("4"),
            status="Planifié",
            description="Analyse MES tentative",
            required_competency="MES",
            planning_type="Flexible",
            priority="Normale",
            confirmation="Tentative",
            confirmation_overridden=False,
            origin="QUICK_SHIFT",
            created_by_name="Coordonnateur Démo",
        ),
    )
    session.add_all(requirements)
    session.flush()

    shifts = (
        Shift(
            id="DEMO-SHIFT-001",
            legacy_allocation_id="DEMO-ALLOC-001",
            resource_requirement_id="DEMO-REQ-001",
            resource_id="DEMO-R-AUTO-1",
            work_date=monday,
            hours=Decimal("8"),
            allocation_type="Flexible",
            source="AUTO",
            locked=False,
            outside_standard_hours=False,
            confirmation=None,
            note="Quart automatique démo",
        ),
        Shift(
            id="DEMO-SHIFT-002",
            legacy_allocation_id="DEMO-ALLOC-002",
            resource_requirement_id="DEMO-REQ-001",
            resource_id="DEMO-R-AUTO-1",
            work_date=monday + timedelta(days=1),
            hours=Decimal("8"),
            allocation_type="Flexible",
            source="MANUAL",
            locked=True,
            outside_standard_hours=False,
            confirmation=None,
            note="Quart manuel verrouillé",
        ),
        Shift(
            id="DEMO-SHIFT-003",
            legacy_allocation_id="DEMO-ALLOC-003",
            resource_requirement_id="DEMO-REQ-001",
            resource_id="DEMO-R-AUTO-1",
            work_date=monday + timedelta(days=2),
            hours=Decimal("8"),
            allocation_type="Flexible",
            source="AUTO",
            locked=False,
            outside_standard_hours=False,
            confirmation=None,
            note=None,
        ),
        Shift(
            id="DEMO-SHIFT-004",
            legacy_allocation_id="DEMO-ALLOC-QS-001",
            resource_requirement_id="DEMO-REQ-QS-001",
            resource_id="DEMO-R-INSTALL-1",
            work_date=monday + timedelta(days=5),
            hours=Decimal("6"),
            allocation_type="Fixe",
            source="MANUAL",
            locked=True,
            outside_standard_hours=True,
            confirmation=None,
            note="Quick Shift hors horaire",
        ),
        Shift(
            id="DEMO-SHIFT-005",
            legacy_allocation_id="DEMO-ALLOC-QS-002",
            resource_requirement_id="DEMO-REQ-QS-002",
            resource_id="DEMO-R-AUTO-2",
            work_date=monday + timedelta(days=4),
            hours=Decimal("4"),
            allocation_type="Flexible",
            source="MANUAL",
            locked=True,
            outside_standard_hours=False,
            confirmation=None,
            note="Charge potentielle démo",
        ),
    )
    session.add_all(shifts)
    session.flush()

    return DemoSeedSummary(
        week_start=monday,
        projects=len(projects),
        resources=len(DEMO_RESOURCE_IDS),
        work_packages=len(work_packages),
        demands=len(requests),
        requirements=len(requirements),
        shifts=len(shifts),
        periods=len(periods),
        users=dev_user_count,
    )


def seed_demo_database(database_url: str, *, today: date | None = None) -> DemoSeedSummary:
    normalized = str(database_url or "").strip()
    if not normalized.casefold().startswith("sqlite"):
        raise ValueError(
            "Le chargeur de données démo est volontairement limité à SQLite."
        )

    engine = create_sql_engine(normalized)
    try:
        required_tables = {
            "projects",
            "resources",
            "work_packages",
            "workforce_requests",
            "request_lines",
            "resource_requirements",
            "shifts",
            "app_users",
        }
        existing = set(inspect(engine).get_table_names())
        missing = sorted(required_tables - existing)
        if missing:
            raise RuntimeError(
                "Schéma incomplet. Exécute d'abord 'alembic upgrade head'. "
                f"Tables manquantes: {', '.join(missing)}"
            )

        factory = create_session_factory(engine)
        with factory.begin() as session:
            return seed_demo_session(session, today=today)
    finally:
        engine.dispose()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recharge un jeu de données de démonstration dans SQLite local."
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("RESOURCEPLANNER_DATABASE_URL", DEFAULT_DATABASE_URL),
        help="URL SQLAlchemy SQLite. Par défaut: base locale RessourcePlanner.",
    )
    parser.add_argument(
        "--confirm-dev-only",
        action="store_true",
        help="Confirmation explicite requise pour utiliser ce seed de développement.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.confirm_dev_only:
        print(
            "ERREUR: ce seed est strictement réservé au développement. "
            "Relancer avec --confirm-dev-only sur une base SQLite locale dédiée."
        )
        return 2
    try:
        summary = seed_demo_database(args.database_url)
    except (ValueError, RuntimeError) as exc:
        print(f"ERREUR: {exc}")
        return 2

    print("Données démo chargées avec succès.")
    print(f"Semaine: {summary.week_start.isoformat()}")
    print(
        "Contenu: "
        f"{summary.projects} projets, "
        f"{summary.resources} ressources, "
        f"{summary.work_packages} WorkPackages, "
        f"{summary.demands} demandes, "
        f"{summary.requirements} besoins, "
        f"{summary.shifts} quarts, "
        f"{summary.periods} périodes, "
        f"{summary.users} identités de développement."
    )
    print("Le rechargement remplace uniquement les données des projets DEMO-*.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
