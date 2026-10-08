"""Repair one already approved legacy simple request's missing planning class.

Dry-run is the default. Only the immutable active approval may supply the class;
candidate edits, resource names, ERP task defaults and automatic assignments are
never used as fallback sources.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.infrastructure.sql.models import ResourceRequirement, WorkforceRequest  # noqa: E402
from app.infrastructure.sql.planning_version import (  # noqa: E402
    SqlPlanningMutationVersionRepository,
)
from app.infrastructure.sql.request_plan_preparation import SqlRequestPlanPreparer  # noqa: E402
from app.infrastructure.sql.session import create_sql_engine  # noqa: E402


def repair_request_class(
    session: Session,
    demand_number: str,
    *,
    apply: bool = False,
) -> dict[str, object]:
    """Restore missing class only; preserve approval, shifts and targeting."""
    if apply:
        SqlPlanningMutationVersionRepository(session).acquire()

    request = session.scalar(
        select(WorkforceRequest).where(
            (WorkforceRequest.legacy_demand_number == demand_number)
            | (WorkforceRequest.id == demand_number)
        )
    )
    if request is None:
        raise ValueError("Demande introuvable.")
    if (
        request.line_mode
        or request.status != "En planification"
        or request.cancellation_state == "ACCEPTED"
    ):
        raise ValueError("Seule une demande simple approuvée et active est admissible.")

    requirements = list(
        session.scalars(
            select(ResourceRequirement).where(
                ResourceRequirement.workforce_request_id == request.id,
                ResourceRequirement.status.not_in(("Annulé", "Terminé")),
            )
        ).all()
    )
    if not requirements:
        raise ValueError("Aucun besoin actif à réparer.")

    preparer = SqlRequestPlanPreparer(session)
    approved = preparer.prepare_active(request, current=requirements)
    matches, obsolete = preparer.match_current(request, requirements, approved.specs)
    if obsolete or len(matches) != len(requirements):
        raise ValueError("Correspondance besoins/révision approuvée ambiguë : aucune écriture.")

    updates: list[tuple[ResourceRequirement, str]] = []
    for match in matches:
        requirement = match.requirement
        class_code = match.spec.required_resource_class
        if (
            requirement is None
            or requirement.approval_revision_id != approved.approval_revision_id
            or requirement.approved_entry_key != match.spec.approved_entry_key
        ):
            raise ValueError("Traçabilité de l'autorisation incomplète : aucune écriture.")
        if not class_code:
            continue
        if requirement.required_resource_class not in (None, class_code):
            raise ValueError("Classe différente déjà persistée : revue manuelle requise.")
        if requirement.required_resource_class is None:
            updates.append((requirement, class_code))

    if apply:
        for requirement, class_code in updates:
            requirement.required_resource_class = class_code
        session.flush()

    return {
        "demand_number": demand_number,
        "approval_revision_id": approved.approval_revision_id,
        "preview": not apply,
        "repaired_count": len(updates),
        "changes": [
            {"requirement_id": row.id, "class_code": class_code}
            for row, class_code in updates
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Réparer la classe d'un besoin simple depuis sa révision approuvée active."
    )
    parser.add_argument("--demand-number", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Appliquer le changement après une prévisualisation et une sauvegarde DB.",
    )
    args = parser.parse_args(argv)
    database_url = os.getenv("RESOURCEPLANNER_DATABASE_URL", "").strip()
    if not database_url:
        print("RESOURCEPLANNER_DATABASE_URL est requis.", file=sys.stderr)
        return 2

    engine = create_sql_engine(database_url)
    try:
        with Session(engine) as session:
            with session.begin():
                report = repair_request_class(
                    session,
                    args.demand_number,
                    apply=args.apply,
                )
        print(
            ("Appliqué" if args.apply else "Prévisualisation")
            + f" : {report['repaired_count']} besoin(s) à corriger."
        )
        for row in report["changes"]:
            print(f"- {row['requirement_id']} : {row['class_code']}")
        return 0
    except (ValueError, SQLAlchemyError) as exc:
        # Avoid printing SQLAlchemy exception details (may contain credentials).
        message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(f"Réparation refusée : {message}", file=sys.stderr)
        return 3
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
