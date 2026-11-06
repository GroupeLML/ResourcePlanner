from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.application.erp_sync_scheduler import ERP_SYNC_PROTOCOL_VERSION
from app.infrastructure.smtp import FernetSecretCipher
from app.infrastructure.sql import (
    ERP_SYNC_CONTROL_ROW_ID,
    ErpSyncBaseline,
    ErpSyncAttempt,
    ErpSyncControl,
    ErpSyncRecoveryState,
    create_session_factory,
    create_sql_engine,
)
from app.server.erp_sync_evidence import ErpSyncEvidenceService
from app.server.runtime import ServerConfigurationError, ServerSettings
from tools.check_server_runtime import (
    ConnectivityReadinessError,
    DriverReadinessError,
    MigrationReadinessError,
    check_database_preflight,
)


class ErpActivationReadinessError(RuntimeError):
    pass


def _check_timezone(settings: ServerSettings) -> dict[str, object]:
    try:
        zone = ZoneInfo(settings.erp_sync.timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ErpActivationReadinessError(
            "Le fuseau ERP configuré n'est pas disponible dans l'image/runtime."
        ) from exc
    if settings.erp_sync.timezone_name != "America/Toronto":
        raise ErpActivationReadinessError(
            "Le calendrier ERP de production doit utiliser America/Toronto."
        )
    return {"status": "ok", "zone": zone.key}


def _cipher(settings: ServerSettings):
    if not settings.config_encryption_key:
        raise ErpActivationReadinessError(
            "RESOURCEPLANNER_CONFIG_ENCRYPTION_KEY est requis pour la preuve ERP."
        )
    try:
        return FernetSecretCipher(settings.config_encryption_key)
    except Exception as exc:
        raise ErpActivationReadinessError(
            "La clé de chiffrement ERP configurée n'est pas utilisable."
        ) from exc


def check_activation_readiness(
    settings: ServerSettings,
    *,
    activation_mode: bool,
    dedicated_account_confirmed: bool,
) -> dict[str, object]:
    database = check_database_preflight(settings.database_url)
    timezone = _check_timezone(settings)

    if settings.acumatica is None:
        raise ErpActivationReadinessError(
            "La source Acumatica serveur-à-serveur n'est pas configurée."
        )
    if not dedicated_account_confirmed:
        raise ErpActivationReadinessError(
            "Le compte de service ERP dédié doit être confirmé hors Git avant activation."
        )

    if activation_mode and not settings.erp_sync.enabled:
        raise ErpActivationReadinessError(
            "Le mode activation exige RESOURCEPLANNER_ERP_SYNC_ENABLED=true."
        )
    if not activation_mode and settings.erp_sync.enabled:
        raise ErpActivationReadinessError(
            "Le préflight avant activation exige encore RESOURCEPLANNER_ERP_SYNC_ENABLED=false."
        )

    cipher = _cipher(settings)
    engine = create_sql_engine(settings.database_url)
    factory = create_session_factory(engine)
    try:
        evidence = ErpSyncEvidenceService(
            factory,
            cipher=cipher,
            settings=settings.erp_sync_evidence,
            control_settings=settings.erp_sync,
        )
        with factory() as session:
            control = session.get(ErpSyncControl, ERP_SYNC_CONTROL_ROW_ID)
            if control is None:
                raise ErpActivationReadinessError(
                    "Le singleton de contrôle ERP est absent."
                )
            if int(control.protocol_version) != ERP_SYNC_PROTOCOL_VERSION:
                raise ErpActivationReadinessError(
                    "Le protocole ERP SQL est incompatible avec l'image courante."
                )
            stored_fingerprint = str(control.config_fingerprint or "").strip()
            if stored_fingerprint and stored_fingerprint != settings.erp_sync.config_fingerprint:
                raise ErpActivationReadinessError(
                    "Le fingerprint ERP SQL ne correspond pas à la configuration de l'image."
                )

            policy = evidence.policy_fingerprint_in_session(session)
            baseline_sources = {
                str(row.source_type)
                for row in session.scalars(
                    select(ErpSyncBaseline).where(
                        ErpSyncBaseline.profile_fingerprint == policy
                    )
                )
            }
            missing_baselines = [
                source_type
                for source_type in ("PROJECTS", "TASKS")
                if source_type not in baseline_sources
            ]
            if missing_baselines:
                raise ErpActivationReadinessError(
                    "Baseline ERP approuvée absente pour: "
                    + ", ".join(missing_baselines)
                )

            recovery_required = session.scalar(
                select(ErpSyncRecoveryState).where(
                    ErpSyncRecoveryState.state == "REQUIRED"
                ).limit(1)
            )
            if recovery_required is not None:
                raise ErpActivationReadinessError(
                    "Une récupération ERP REQUIRED bloque l'activation."
                )

            unresolved_review = session.scalar(
                select(ErpSyncAttempt).where(
                    ErpSyncAttempt.status == "PENDING",
                    ErpSyncAttempt.phase == "PLAUSIBILITY_REVIEW_REQUIRED",
                ).limit(1)
            )
            if unresolved_review is not None:
                raise ErpActivationReadinessError(
                    "Une revue de plausibilité ERP reste non résolue."
                )

        if not evidence.automatic_execution_available(
            profile_fingerprint=settings.erp_sync.config_fingerprint
        ):
            raise ErpActivationReadinessError(
                "La capacité automatique 643B n'est pas prête."
            )

        return {
            "status": "ready",
            "mode": "activation" if activation_mode else "pre_activation",
            "database": database,
            "timezone": timezone,
            "protocol_version": ERP_SYNC_PROTOCOL_VERSION,
            "config_fingerprint": settings.erp_sync.config_fingerprint,
            "evidence_budget_mib": settings.erp_sync_evidence.evidence_budget_mib,
            "failure_retention_days": settings.erp_sync_evidence.failure_retention_days,
            "history_days": settings.erp_sync_evidence.history_days,
            "baselines": ["PROJECTS", "TASKS"],
            "recovery_required": False,
            "dedicated_account_confirmed": True,
            "automatic_enabled": settings.erp_sync.enabled,
        }
    finally:
        engine.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fail-closed 643C ERP production activation readiness."
    )
    parser.add_argument(
        "--activation",
        action="store_true",
        help="Validate the final enabled configuration. Without this flag, require automatic sync disabled.",
    )
    parser.add_argument(
        "--dedicated-account-confirmed",
        action="store_true",
        help="Operator attestation that the dedicated read-only ERP service account was verified outside Git.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        settings = ServerSettings.from_environment()
        result = check_activation_readiness(
            settings,
            activation_mode=bool(args.activation),
            dedicated_account_confirmed=bool(args.dedicated_account_confirmed),
        )
    except ServerConfigurationError as exc:
        print(json.dumps({"status": "configuration_error", "message": str(exc)}, ensure_ascii=False))
        return 2
    except (DriverReadinessError, ConnectivityReadinessError, MigrationReadinessError) as exc:
        print(json.dumps({"status": "database_error", "message": str(exc)}, ensure_ascii=False))
        return 3
    except ErpActivationReadinessError as exc:
        print(json.dumps({"status": "blocked", "message": str(exc)}, ensure_ascii=False))
        return 4
    except Exception as exc:
        print(
            json.dumps(
                {"status": "error", "message": "Échec inattendu du préflight ERP.", "type": type(exc).__name__},
                ensure_ascii=False,
            )
        )
        return 5

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
