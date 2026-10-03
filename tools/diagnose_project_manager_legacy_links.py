from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.application.project_manager_legacy_diagnostic import (  # noqa: E402
    LegacyProjectManagerDiagnosticReport,
    LegacyProjectManagerDiagnosticService,
)
from app.application.project_managers import ProjectManagerResolutionService  # noqa: E402
from app.infrastructure.sql.project_manager_legacy_diagnostic_repository import (  # noqa: E402
    SqlLegacyProjectManagerDiagnosticRepository,
)
from app.infrastructure.sql.project_manager_resolution_repository import (  # noqa: E402
    SqlProjectManagerResolutionRepository,
)
from app.infrastructure.sql.session import create_sql_engine  # noqa: E402


DATABASE_URL_ENV = "RESOURCEPLANNER_DATABASE_URL"


def report_to_dict(
    report: LegacyProjectManagerDiagnosticReport,
) -> dict[str, object]:
    return {
        "status": "review_required" if report.review_required else "ok",
        "mutation_performed": False,
        "summary": asdict(report.summary),
        "projects": [asdict(row) for row in report.projects],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Diagnostiquer Project.project_manager_contact_id sans mutation. "
            "Le principal canonique reste résolu depuis l'identité EmployeID ERP."
        )
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Émettre le rapport structuré JSON.",
    )
    parser.add_argument(
        "--fail-on-review",
        action="store_true",
        help=(
            "Retourner le code 3 lorsqu'une divergence, une référence cassée, "
            "un principal canonique non résolu ou un legacy inactif exige une revue."
        ),
    )
    return parser


def _print_human(payload: dict[str, object]) -> None:
    summary = payload["summary"]
    assert isinstance(summary, dict)
    print(f"Legacy project-manager diagnostic: {payload['status']}")
    for key in (
        "projects_scanned",
        "legacy_null",
        "legacy_matching",
        "legacy_divergent",
        "legacy_broken",
        "canonical_unresolved",
        "legacy_inactive",
    ):
        print(f"- {key}: {summary[key]}")
    projects = payload["projects"]
    assert isinstance(projects, list)
    for row in projects:
        assert isinstance(row, dict)
        if row["classification"] == "LEGACY_ABSENT":
            continue
        primary = row.get("canonical_primary")
        canonical_contact_id = (
            primary.get("business_contact_id")
            if isinstance(primary, dict)
            else None
        )
        print(
            "- "
            f"{row['project_number']} [{row['classification']}] "
            f"canonical_contact={canonical_contact_id or '-'} "
            f"legacy_contact={row['legacy_project_manager_contact_id'] or '-'}"
        )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    database_url = str(os.getenv(DATABASE_URL_ENV) or "").strip()
    if not database_url:
        payload = {
            "status": "configuration_error",
            "mutation_performed": False,
            "message": f"{DATABASE_URL_ENV} est requis.",
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 2

    engine = create_sql_engine(database_url)
    try:
        try:
            with Session(engine) as session:
                service = LegacyProjectManagerDiagnosticService(
                    SqlLegacyProjectManagerDiagnosticRepository(session),
                    ProjectManagerResolutionService(
                        SqlProjectManagerResolutionRepository(session)
                    ),
                )
                report = service.inspect()
        except SQLAlchemyError as exc:
            payload = {
                "status": "database_error",
                "mutation_performed": False,
                "message": type(exc).__name__,
            }
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 4
    finally:
        engine.dispose()

    payload = report_to_dict(report)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        _print_human(payload)

    if args.fail_on_review and report.review_required:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
