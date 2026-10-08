"""Restore missing workforce class on one or all approved legacy simple requests.

Dry-run is the default. Only the immutable active approval may supply a class;
candidate edits, resource names, ERP task defaults and automatic assignments
are never used as fallback sources. No shifts or approval records are changed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from sqlalchemy import false, select
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
        "demand_number": request.legacy_demand_number or request.id,
        "approval_revision_id": approved.approval_revision_id,
        "preview": not apply,
        "repaired_count": len(updates),
        "changes": [
            {"requirement_id": row.id, "class_code": class_code}
            for row, class_code in updates
        ],
    }


def _unclassified_request_ids_statement():
    """Portable predicate: SQL Server requires `= 0`, not `IS 0`."""
    return (
        select(WorkforceRequest.id)
        .join(
            ResourceRequirement,
            ResourceRequirement.workforce_request_id == WorkforceRequest.id,
        )
        .where(
            WorkforceRequest.line_mode == false(),
            WorkforceRequest.status == "En planification",
            ResourceRequirement.status.not_in(("Annulé", "Terminé")),
            ResourceRequirement.required_resource_class.is_(None),
        )
        .distinct()
        .order_by(WorkforceRequest.id)
    )


def repair_all_request_classes(
    session: Session,
    *,
    apply: bool = False,
) -> dict[str, object]:
    """Inspect every active simple request with an unclassified active need.

    All safe updates run under one transaction and planning CAS on apply.
    Unverifiable requests are reported for review and left unchanged.
    """
    if apply:
        # Acquire before querying, so a concurrent planning mutation cannot
        # change the scanned requirements while the repair is running.
        SqlPlanningMutationVersionRepository(session).acquire()

    request_ids = session.scalars(_unclassified_request_ids_statement()).all()
    changed: list[dict[str, object]] = []
    skipped: list[dict[str, str]] = []
    unchanged = 0

    for request_id in request_ids:
        try:
            report = repair_request_class(session, request_id, apply=apply)
        except ValueError as exc:
            # A missing/ambiguous approval is never a reason to infer a class.
            # Errors in one historic request must not modify that request.
            skipped.append({"request_id": request_id, "reason": str(exc)})
            continue
        if report["repaired_count"]:
            changed.append(report)
        else:
            unchanged += 1

    return {
        "preview": not apply,
        "requests_scanned": len(request_ids),
        "requests_corrected": len(changed),
        "requirements_corrected": sum(
            int(row["repaired_count"]) for row in changed
        ),
        "requests_unchanged": unchanged,
        "requests_needing_review": len(skipped),
        "changed_requests": changed,
        "skipped_requests": skipped,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Restaurer la classe des besoins simples depuis les révisions approuvées actives."
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--demand-number", help="Réparer une demande précise.")
    target.add_argument(
        "--all",
        action="store_true",
        help="Examiner toutes les demandes simples approuvées ayant un besoin sans classe.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Appliquer les corrections prouvées après prévisualisation et sauvegarde DB.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Sortie structurée avec demandes ignorées et raisons.",
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
                report = (
                    repair_all_request_classes(session, apply=args.apply)
                    if args.all
                    else repair_request_class(
                        session,
                        args.demand_number,
                        apply=args.apply,
                    )
                )
        if args.json:
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        elif args.all:
            print(
                ("Appliqué" if args.apply else "Prévisualisation")
                + f" : {report['requests_scanned']} demande(s) examinée(s), "
                + f"{report['requests_corrected']} demande(s) et "
                + f"{report['requirements_corrected']} besoin(s) corrigible(s), "
                + f"{report['requests_unchanged']} sans modification, "
                + f"{report['requests_needing_review']} à vérifier manuellement."
            )
            for row in report["changed_requests"]:
                print(f"- {row['demand_number']} : {row['repaired_count']} besoin(s)")
            for row in report["skipped_requests"]:
                print(f"! {row['request_id']} : {row['reason']}")
        else:
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
