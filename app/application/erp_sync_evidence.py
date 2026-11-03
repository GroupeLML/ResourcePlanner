from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from typing import Any, Mapping, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .errors import (
    ApplicationConflictError,
    ApplicationUnavailableError,
    ApplicationValidationError,
)
from .smtp_settings import SecretCipherPort


ERP_EVIDENCE_CHUNK_BYTES = 512 * 1024


@dataclass(frozen=True, slots=True)
class ErpSyncEvidenceSettings:
    min_source_ratio: float = 0.50
    min_projects: int = 1
    min_task_rows: int = 1
    max_evidence_mib: int = 256
    evidence_budget_mib: int = 2048
    failure_retention_days: int = 14
    history_days: int = 90

    def __post_init__(self) -> None:
        if not 0 < float(self.min_source_ratio) <= 1:
            raise ValueError("min_source_ratio must be > 0 and <= 1")
        if int(self.min_projects) < 0 or int(self.min_task_rows) < 0:
            raise ValueError("source minimums must be >= 0")
        if not 16 <= int(self.max_evidence_mib) <= 1024:
            raise ValueError("max_evidence_mib must be between 16 and 1024")
        if not int(self.max_evidence_mib) <= int(self.evidence_budget_mib) <= 16384:
            raise ValueError("evidence_budget_mib must be >= attempt max and <= 16384")
        if not 1 <= int(self.failure_retention_days) <= 90:
            raise ValueError("failure_retention_days must be between 1 and 90")
        if not 30 <= int(self.history_days) <= 365:
            raise ValueError("history_days must be between 30 and 365")

    @property
    def max_evidence_bytes(self) -> int:
        return int(self.max_evidence_mib) * 1024 * 1024

    @property
    def evidence_budget_bytes(self) -> int:
        return int(self.evidence_budget_mib) * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ErpSyncCapturedSource:
    source_type: str
    manifest_id: str
    source_fingerprint: str
    row_count: int
    project_counts: Mapping[str, int]
    payload: Any


@dataclass(frozen=True, slots=True)
class ErpSyncPlausibilityResult:
    accepted: bool
    diagnostics: tuple[str, ...]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def content_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _safe_reason(value: str) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise ApplicationValidationError(
            "Une raison explicite est requise.",
            code="erp_sync_evidence_reason_required",
        )
    return text[:1000]


class EvidenceBackedErpSyncAutomaticSafetyCapability:
    """Advertise automatic safety only when the complete 643B prerequisite set is ready."""

    def __init__(
        self,
        service: "ErpSyncEvidenceService",
        *,
        profile_fingerprint: str,
    ) -> None:
        self._service = service
        self._profile_fingerprint = profile_fingerprint

    def automatic_execution_available(self) -> bool:
        return self._service.automatic_execution_available(
            profile_fingerprint=self._profile_fingerprint
        )

    def diagnostic(self) -> str:
        return "erp_sync_643b_safety_not_ready"


class ErpSyncEvidenceService:
    """643B durable source/checkpoint evidence around the 643A control plane."""

    def __init__(
        self,
        session_factory,
        *,
        cipher: SecretCipherPort | None,
        settings: ErpSyncEvidenceSettings,
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher
        self.settings = settings

    @property
    def crypto_available(self) -> bool:
        return self._cipher is not None

    def _require_cipher(self) -> SecretCipherPort:
        if self._cipher is None:
            raise ApplicationUnavailableError(
                "Le chiffrement des preuves ERP n'est pas configuré.",
                code="erp_sync_evidence_crypto_unavailable",
            )
        return self._cipher

    def _encrypt_bytes(self, payload: bytes) -> str:
        cipher = self._require_cipher()
        return cipher.encrypt(payload.decode("utf-8"))

    def _decrypt_bytes(self, encrypted: str) -> bytes:
        cipher = self._require_cipher()
        try:
            return cipher.decrypt(encrypted).encode("utf-8")
        except Exception as exc:
            raise ApplicationConflictError(
                "Une preuve ERP ne peut pas être déchiffrée.",
                code="erp_sync_evidence_decrypt_failed",
            ) from exc

    def reserve_attempt(self, *, attempt_id: str) -> None:
        from ..infrastructure.sql import ErpSyncEvidenceReservation
        from ..infrastructure.sql.session import transactional_session

        requested = self.settings.max_evidence_bytes
        with transactional_session(self._session_factory) as session:
            existing = session.scalar(
                select(ErpSyncEvidenceReservation).where(
                    ErpSyncEvidenceReservation.attempt_id == attempt_id
                )
            )
            if existing is not None:
                return
            active_reserved = int(
                session.scalar(
                    select(func.coalesce(func.sum(ErpSyncEvidenceReservation.reserved_bytes), 0))
                    .where(ErpSyncEvidenceReservation.released_at.is_(None))
                )
                or 0
            )
            if active_reserved + requested > self.settings.evidence_budget_bytes:
                raise ApplicationUnavailableError(
                    "Le budget de stockage des preuves ERP est insuffisant.",
                    code="erp_sync_evidence_storage_budget_unavailable",
                )
            session.add(
                ErpSyncEvidenceReservation(
                    attempt_id=attempt_id,
                    reserved_bytes=requested,
                    used_bytes=0,
                )
            )

    def _consume_bytes(self, session: Session, *, attempt_id: str, amount: int) -> None:
        from ..infrastructure.sql import ErpSyncEvidenceReservation

        reservation = session.scalar(
            select(ErpSyncEvidenceReservation).where(
                ErpSyncEvidenceReservation.attempt_id == attempt_id
            )
        )
        if reservation is None:
            raise ApplicationConflictError(
                "La tentative ERP ne possède pas de réservation de preuve.",
                code="erp_sync_evidence_reservation_missing",
            )
        next_used = int(reservation.used_bytes) + max(0, int(amount))
        if next_used > int(reservation.reserved_bytes):
            raise ApplicationUnavailableError(
                "La tentative ERP dépasse son budget de preuve réservé.",
                code="erp_sync_evidence_attempt_budget_exceeded",
            )
        reservation.used_bytes = next_used

    def capture_source(
        self,
        *,
        attempt_id: str,
        source_type: str,
        profile_fingerprint: str,
        payload: Any,
        row_count: int,
        project_counts: Mapping[str, int] | None = None,
    ) -> ErpSyncCapturedSource:
        from ..infrastructure.sql import (
            EVIDENCE_PROOF_SOURCE,
            EVIDENCE_STATE_SEALED,
            ERP_EVIDENCE_PROTOCOL_VERSION,
            ErpSyncEvidenceChunk,
            ErpSyncEvidenceManifest,
        )
        from ..infrastructure.sql.session import transactional_session

        self.reserve_attempt(attempt_id=attempt_id)
        normalized = canonical_json_bytes(payload)
        if len(normalized) > self.settings.max_evidence_bytes:
            raise ApplicationUnavailableError(
                "La source ERP dépasse la taille maximale de preuve par tentative.",
                code="erp_sync_evidence_attempt_budget_exceeded",
            )
        digest = hashlib.sha256(normalized).hexdigest()
        chunks = tuple(
            normalized[offset : offset + ERP_EVIDENCE_CHUNK_BYTES]
            for offset in range(0, len(normalized), ERP_EVIDENCE_CHUNK_BYTES)
        ) or (b"[]",)
        observed_at = _utc_now()
        encrypted_chunks: list[tuple[str, str, int, int]] = []
        ciphertext_total = 0
        for chunk in chunks:
            encrypted = self._encrypt_bytes(chunk)
            ciphertext_bytes = len(encrypted.encode("utf-8"))
            ciphertext_total += ciphertext_bytes
            encrypted_chunks.append(
                (
                    encrypted,
                    hashlib.sha256(chunk).hexdigest(),
                    len(chunk),
                    ciphertext_bytes,
                )
            )

        with transactional_session(self._session_factory) as session:
            existing = session.scalar(
                select(ErpSyncEvidenceManifest).where(
                    ErpSyncEvidenceManifest.attempt_id == attempt_id,
                    ErpSyncEvidenceManifest.source_type == source_type,
                    ErpSyncEvidenceManifest.proof_type == EVIDENCE_PROOF_SOURCE,
                )
            )
            if existing is not None:
                if existing.source_fingerprint != digest:
                    raise ApplicationConflictError(
                        "Une source différente est déjà scellée pour cette tentative.",
                        code="erp_sync_evidence_source_conflict",
                    )
                return ErpSyncCapturedSource(
                    source_type=source_type,
                    manifest_id=existing.id,
                    source_fingerprint=digest,
                    row_count=int(existing.row_count),
                    project_counts=dict(project_counts or {}),
                    payload=payload,
                )

            self._consume_bytes(
                session,
                attempt_id=attempt_id,
                amount=ciphertext_total,
            )
            manifest = ErpSyncEvidenceManifest(
                attempt_id=attempt_id,
                protocol_version=ERP_EVIDENCE_PROTOCOL_VERSION,
                source_type=source_type,
                proof_type=EVIDENCE_PROOF_SOURCE,
                profile_fingerprint=profile_fingerprint,
                source_fingerprint=digest,
                observed_at=observed_at,
                row_count=max(0, int(row_count)),
                chunk_count=len(encrypted_chunks),
                plaintext_bytes=len(normalized),
                ciphertext_bytes=ciphertext_total,
                content_sha256=digest,
                state=EVIDENCE_STATE_SEALED,
            )
            session.add(manifest)
            session.flush()
            for ordinal, (encrypted, chunk_hash, plain_size, cipher_size) in enumerate(
                encrypted_chunks
            ):
                session.add(
                    ErpSyncEvidenceChunk(
                        manifest_id=manifest.id,
                        ordinal=ordinal,
                        encrypted_payload=encrypted,
                        plaintext_sha256=chunk_hash,
                        plaintext_bytes=plain_size,
                        ciphertext_bytes=cipher_size,
                    )
                )
            session.flush()
            manifest_id = manifest.id

        # Verify decryptability/integrity before a source may be used for mutation.
        self.load_source(manifest_id=manifest_id)
        return ErpSyncCapturedSource(
            source_type=source_type,
            manifest_id=manifest_id,
            source_fingerprint=digest,
            row_count=max(0, int(row_count)),
            project_counts=dict(project_counts or {}),
            payload=payload,
        )

    def load_source(self, *, manifest_id: str) -> Any:
        from ..infrastructure.sql import (
            EVIDENCE_STATE_PURGED,
            ERP_EVIDENCE_PROTOCOL_VERSION,
            ErpSyncEvidenceChunk,
            ErpSyncEvidenceManifest,
        )

        with self._session_factory() as session:
            manifest = session.get(ErpSyncEvidenceManifest, manifest_id)
            if manifest is None:
                raise ApplicationValidationError(
                    "La preuve ERP est introuvable.",
                    code="erp_sync_evidence_not_found",
                )
            if int(manifest.protocol_version) != ERP_EVIDENCE_PROTOCOL_VERSION:
                raise ApplicationConflictError(
                    "La version de protocole de preuve ERP est incompatible.",
                    code="erp_sync_evidence_protocol_incompatible",
                )
            if manifest.state == EVIDENCE_STATE_PURGED:
                raise ApplicationConflictError(
                    "La preuve ERP a été purgée.",
                    code="erp_sync_evidence_purged",
                )
            chunks = tuple(
                session.scalars(
                    select(ErpSyncEvidenceChunk)
                    .where(ErpSyncEvidenceChunk.manifest_id == manifest_id)
                    .order_by(ErpSyncEvidenceChunk.ordinal)
                )
            )
            payload = b"".join(self._decrypt_bytes(row.encrypted_payload) for row in chunks)
            if hashlib.sha256(payload).hexdigest() != manifest.content_sha256:
                raise ApplicationConflictError(
                    "L'intégrité de la preuve ERP est invalide.",
                    code="erp_sync_evidence_integrity_invalid",
                )
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApplicationConflictError(
                "Le contenu normalisé de la preuve ERP est invalide.",
                code="erp_sync_evidence_payload_invalid",
            ) from exc

    def approve_baseline(
        self,
        *,
        manifest_id: str,
        actor_name: str,
        reason: str,
        project_counts: Mapping[str, int] | None = None,
    ) -> str:
        from ..infrastructure.sql import (
            EVIDENCE_SOURCE_PROJECTS,
            EVIDENCE_SOURCE_TASKS,
            EVIDENCE_STATE_SEALED,
            ErpSyncBaseline,
            ErpSyncEvidenceManifest,
        )
        from ..infrastructure.sql.base import new_id
        from ..infrastructure.sql.session import transactional_session

        actor = " ".join(str(actor_name or "").split())
        if not actor:
            raise ApplicationValidationError(
                "Un acteur est requis pour approuver le baseline ERP.",
                code="erp_sync_baseline_actor_required",
            )
        reason_value = _safe_reason(reason)
        with transactional_session(self._session_factory) as session:
            manifest = session.get(ErpSyncEvidenceManifest, manifest_id)
            if manifest is None or manifest.state != EVIDENCE_STATE_SEALED:
                raise ApplicationConflictError(
                    "Seule une source ERP scellée peut devenir baseline.",
                    code="erp_sync_baseline_source_not_sealed",
                )
            if manifest.source_type not in {
                EVIDENCE_SOURCE_PROJECTS,
                EVIDENCE_SOURCE_TASKS,
            }:
                raise ApplicationValidationError(
                    "Le type de source ne peut pas servir de baseline.",
                    code="erp_sync_baseline_source_invalid",
                )
            encrypted_counts = None
            if project_counts is not None:
                encrypted_counts = self._encrypt_bytes(canonical_json_bytes(project_counts))
            baseline = session.scalar(
                select(ErpSyncBaseline).where(
                    ErpSyncBaseline.source_type == manifest.source_type,
                    ErpSyncBaseline.profile_fingerprint == manifest.profile_fingerprint,
                )
            )
            if baseline is None:
                baseline = ErpSyncBaseline(
                    id=new_id(),
                    source_type=manifest.source_type,
                    profile_fingerprint=manifest.profile_fingerprint,
                    source_fingerprint=str(manifest.source_fingerprint),
                    row_count=int(manifest.row_count),
                    encrypted_project_counts=encrypted_counts,
                    approved_by=actor,
                    approval_reason=reason_value,
                    approved_at=_utc_now(),
                )
                session.add(baseline)
            else:
                baseline.source_fingerprint = str(manifest.source_fingerprint)
                baseline.row_count = int(manifest.row_count)
                baseline.encrypted_project_counts = encrypted_counts
                baseline.approved_by = actor
                baseline.approval_reason = reason_value
                baseline.approved_at = _utc_now()
            session.flush()
            return baseline.id

    def _baseline_project_counts(self, baseline) -> dict[str, int]:
        if not baseline.encrypted_project_counts:
            return {}
        try:
            value = json.loads(
                self._decrypt_bytes(baseline.encrypted_project_counts).decode("utf-8")
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApplicationConflictError(
                "Le baseline ERP ne peut pas être vérifié.",
                code="erp_sync_baseline_integrity_invalid",
            ) from exc
        if not isinstance(value, dict):
            return {}
        return {str(key): int(count) for key, count in value.items()}

    def validate_plausibility(
        self,
        captured: ErpSyncCapturedSource,
        *,
        active_task_targets: int = 0,
    ) -> ErpSyncPlausibilityResult:
        from ..infrastructure.sql import ErpSyncBaseline

        with self._session_factory() as session:
            baseline = session.scalar(
                select(ErpSyncBaseline).where(
                    ErpSyncBaseline.source_type == captured.source_type,
                    ErpSyncBaseline.profile_fingerprint
                    == self._manifest_profile(session, captured.manifest_id),
                )
            )
            if baseline is None:
                return ErpSyncPlausibilityResult(
                    accepted=False,
                    diagnostics=("erp_sync_baseline_required",),
                )
            diagnostics: list[str] = []
            floor = (
                self.settings.min_projects
                if captured.source_type == "PROJECTS"
                else self.settings.min_task_rows if active_task_targets else 0
            )
            if captured.row_count < floor:
                diagnostics.append("erp_sync_source_below_floor")
            baseline_count = int(baseline.row_count)
            if baseline_count > 0 and (
                captured.row_count / baseline_count < self.settings.min_source_ratio
            ):
                diagnostics.append("erp_sync_source_ratio_too_low")
            baseline_projects = self._baseline_project_counts(baseline)
            for project, previous in baseline_projects.items():
                if previous <= 0:
                    continue
                current = int(captured.project_counts.get(project, 0))
                if current / previous < self.settings.min_source_ratio:
                    diagnostics.append(f"erp_sync_project_ratio_too_low:{project}")
            return ErpSyncPlausibilityResult(
                accepted=not diagnostics,
                diagnostics=tuple(diagnostics),
            )

    @staticmethod
    def _manifest_profile(session: Session, manifest_id: str) -> str:
        from ..infrastructure.sql import ErpSyncEvidenceManifest

        manifest = session.get(ErpSyncEvidenceManifest, manifest_id)
        if manifest is None:
            raise ApplicationValidationError(
                "La preuve ERP est introuvable.",
                code="erp_sync_evidence_not_found",
            )
        return str(manifest.profile_fingerprint)

    def automatic_execution_available(self, *, profile_fingerprint: str) -> bool:
        if self._cipher is None:
            return False
        from ..infrastructure.sql import ErpSyncBaseline

        with self._session_factory() as session:
            count = int(
                session.scalar(
                    select(func.count(ErpSyncBaseline.id)).where(
                        ErpSyncBaseline.profile_fingerprint == profile_fingerprint,
                        ErpSyncBaseline.source_type.in_(("PROJECTS", "TASKS")),
                    )
                )
                or 0
            )
            return count == 2

    def release_reservation(self, *, attempt_id: str) -> None:
        from ..infrastructure.sql import ErpSyncEvidenceReservation
        from ..infrastructure.sql.session import transactional_session

        with transactional_session(self._session_factory) as session:
            row = session.scalar(
                select(ErpSyncEvidenceReservation).where(
                    ErpSyncEvidenceReservation.attempt_id == attempt_id
                )
            )
            if row is not None and row.released_at is None:
                row.released_at = _utc_now()

    def purge_expired(self) -> int:
        from ..infrastructure.sql import (
            EVIDENCE_STATE_EXPIRED,
            EVIDENCE_STATE_PURGED,
            ErpSyncEvidenceChunk,
            ErpSyncEvidenceManifest,
        )
        from ..infrastructure.sql.session import transactional_session

        cutoff = _utc_now() - timedelta(days=self.settings.history_days)
        purged = 0
        with transactional_session(self._session_factory) as session:
            rows = tuple(
                session.scalars(
                    select(ErpSyncEvidenceManifest).where(
                        ErpSyncEvidenceManifest.pinned.is_(False),
                        ErpSyncEvidenceManifest.created_at < cutoff,
                        ErpSyncEvidenceManifest.state.not_in((EVIDENCE_STATE_PURGED,)),
                    )
                )
            )
            for row in rows:
                session.query(ErpSyncEvidenceChunk).filter(
                    ErpSyncEvidenceChunk.manifest_id == row.id
                ).delete(synchronize_session=False)
                row.state = EVIDENCE_STATE_PURGED
                row.diagnostic = "erp_sync_evidence_retention_purged"
                purged += 1
        return purged
