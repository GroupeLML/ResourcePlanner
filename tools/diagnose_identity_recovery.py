from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import sys

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.infrastructure.sql import (  # noqa: E402
    AppUser,
    ErpUserDirectoryEntry,
    create_sql_engine,
)


DATABASE_URL_ENV = "RESOURCEPLANNER_DATABASE_URL"


@dataclass(frozen=True, slots=True)
class IdentityRecoveryDiagnostic:
    app_user_id: str
    employee_external_id: str | None
    erp_user_id: str | None
    candidate_erp_user_ids: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["candidate_erp_user_ids"] = list(self.candidate_erp_user_ids)
        return payload


def _optional(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _candidate_erp_user_ids(
    session: Session,
    employee_external_id: str,
) -> tuple[str, ...]:
    return tuple(
        session.scalars(
            select(ErpUserDirectoryEntry.user_id)
            .where(
                ErpUserDirectoryEntry.employee_external_id
                == employee_external_id
            )
            .order_by(ErpUserDirectoryEntry.user_id)
        ).all()
    )


def inspect_identity_recovery(
    session: Session,
) -> tuple[IdentityRecoveryDiagnostic, ...]:
    """Return unresolved historical OIDC/AppUser links without mutating identity data."""

    rows = session.scalars(
        select(AppUser)
        .where(
            AppUser.issuer.is_not(None),
            AppUser.subject.is_not(None),
        )
        .order_by(AppUser.id)
    ).all()
    diagnostics: list[IdentityRecoveryDiagnostic] = []

    for row in rows:
        employee_external_id = _optional(row.employee_external_id)
        erp_user_id = _optional(row.erp_user_id)

        if erp_user_id is not None:
            directory = session.get(ErpUserDirectoryEntry, erp_user_id)
            if directory is None:
                diagnostics.append(
                    IdentityRecoveryDiagnostic(
                        app_user_id=row.id,
                        employee_external_id=employee_external_id,
                        erp_user_id=erp_user_id,
                        candidate_erp_user_ids=(),
                        reason="linked_erp_user_missing",
                    )
                )
                continue
            directory_employee = _optional(directory.employee_external_id)
            if (
                employee_external_id is None
                or directory_employee is None
                or directory_employee != employee_external_id
            ):
                diagnostics.append(
                    IdentityRecoveryDiagnostic(
                        app_user_id=row.id,
                        employee_external_id=employee_external_id,
                        erp_user_id=erp_user_id,
                        candidate_erp_user_ids=(erp_user_id,),
                        reason="employee_external_id_mismatch",
                    )
                )
            continue

        if employee_external_id is None:
            diagnostics.append(
                IdentityRecoveryDiagnostic(
                    app_user_id=row.id,
                    employee_external_id=None,
                    erp_user_id=None,
                    candidate_erp_user_ids=(),
                    reason="employee_external_id_missing",
                )
            )
            continue

        candidates = _candidate_erp_user_ids(session, employee_external_id)
        if not candidates:
            reason = "erp_user_not_found"
        elif len(candidates) > 1:
            reason = "erp_user_ambiguous"
        else:
            owner = session.scalar(
                select(AppUser.id).where(AppUser.erp_user_id == candidates[0])
            )
            reason = (
                "erp_user_already_owned"
                if owner is not None and owner != row.id
                else "deterministic_backfill_pending"
            )

        diagnostics.append(
            IdentityRecoveryDiagnostic(
                app_user_id=row.id,
                employee_external_id=employee_external_id,
                erp_user_id=None,
                candidate_erp_user_ids=candidates,
                reason=reason,
            )
        )

    return tuple(diagnostics)


def apply_deterministic_identity_recovery(
    session: Session,
) -> tuple[str, ...]:
    """Backfill only exact unowned EmployeID -> UserID matches.

    The caller owns the transaction. Any database constraint/concurrency failure
    propagates so the transaction can roll back instead of partially guessing.
    """

    recovered: list[str] = []
    for diagnostic in inspect_identity_recovery(session):
        if diagnostic.reason != "deterministic_backfill_pending":
            continue
        if (
            diagnostic.employee_external_id is None
            or len(diagnostic.candidate_erp_user_ids) != 1
        ):
            continue

        row = session.get(AppUser, diagnostic.app_user_id)
        if row is None or _optional(row.erp_user_id) is not None:
            continue

        candidates = _candidate_erp_user_ids(
            session,
            diagnostic.employee_external_id,
        )
        if candidates != diagnostic.candidate_erp_user_ids:
            continue

        candidate = candidates[0]
        owner = session.scalar(
            select(AppUser.id).where(AppUser.erp_user_id == candidate)
        )
        if owner is not None and owner != row.id:
            continue

        row.erp_user_id = candidate
        recovered.append(row.id)

    session.flush()
    return tuple(recovered)


def build_report(
    session: Session,
    *,
    recovered_app_user_ids: tuple[str, ...] = (),
) -> dict[str, object]:
    diagnostics = inspect_identity_recovery(session)
    return {
        "status": "ok" if not diagnostics else "review_required",
        "unresolved_count": len(diagnostics),
        "diagnostics": [item.to_dict() for item in diagnostics],
        "mutation_performed": bool(recovered_app_user_ids),
        "recovered_count": len(recovered_app_user_ids),
        "recovered_app_user_ids": list(recovered_app_user_ids),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Diagnostiquer la reprise des identités historiques ADR-012. "
            "Le mode par défaut est strictement read-only."
        )
    )
    parser.add_argument(
        "--apply-deterministic",
        action="store_true",
        help=(
            "Renseigner uniquement AppUser.erp_user_id lorsque l'EmployeID "
            "correspond à exactement un UserID non possédé."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    database_url = str(os.getenv(DATABASE_URL_ENV) or "").strip()
    if not database_url:
        print(
            json.dumps(
                {
                    "status": "configuration_error",
                    "message": f"{DATABASE_URL_ENV} est requis.",
                    "mutation_performed": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2

    engine = create_sql_engine(database_url)
    recovered: tuple[str, ...] = ()
    try:
        if args.apply_deterministic:
            try:
                with Session(engine) as session:
                    with session.begin():
                        recovered = apply_deterministic_identity_recovery(session)
            except SQLAlchemyError:
                print(
                    json.dumps(
                        {
                            "status": "recovery_error",
                            "message": (
                                "La reprise déterministe a échoué; la transaction "
                                "a été annulée sans résolution partielle."
                            ),
                            "mutation_performed": False,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                return 4

        with Session(engine) as session:
            report = build_report(
                session,
                recovered_app_user_ids=recovered,
            )
    finally:
        engine.dispose()

    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "ok" else 3


if __name__ == "__main__":
    raise SystemExit(main())
