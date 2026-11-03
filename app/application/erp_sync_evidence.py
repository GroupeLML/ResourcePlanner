from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from typing import Any, Mapping, Sequence

from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from .errors import (
    ApplicationConflictError,
    ApplicationUnavailableError,
    ApplicationValidationError,
)
from .erp_sync_scheduler import ErpSyncSchedulerSettings
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
        control_settings: ErpSyncSchedulerSettings | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher
        self.settings = settings
        self._control_settings = control_settings

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
        from ..infrastructure.sql import ErpSyncAttempt, ErpSyncEvidenceReservation
        from ..infrastructure.sql.erp_sync_control_repository import (
            SqlErpSyncControlRepository,
        )
        from ..infrastructure.sql.session import transactional_session

        requested = self.settings.max_evidence_bytes
        with transactional_session(self._session_factory) as session:
            if self._control_settings is None:
                raise ApplicationUnavailableError(
                    "Le profil de contrôle ERP requis pour la réservation de preuve est absent.",
                    code="erp_sync_evidence_control_unavailable",
                )
            attempt = session.get(ErpSyncAttempt, attempt_id)
            if attempt is None:
                raise ApplicationValidationError(
                    "La tentative ERP est introuvable.",
                    code="erp_sync_attempt_not_found",
                )
            SqlErpSyncControlRepository.guard_session(
                session,
                attempt_id=attempt_id,
                generation=int(attempt.generation),
                settings=self._control_settings,
            )
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
            # Released reservations retain the actual ciphertext/accounting used
            # by source chunks and mutation evidence until their retention purge.
            retained_used = int(
                session.scalar(
                    select(func.coalesce(func.sum(ErpSyncEvidenceReservation.used_bytes), 0))
                    .where(ErpSyncEvidenceReservation.released_at.is_not(None))
                )
                or 0
            )
            if retained_used + active_reserved + requested > self.settings.evidence_budget_bytes:
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
                amount=ciphertext_total + 1024,
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

    def record_before_in_session(
        self,
        session: Session,
        *,
        attempt_id: str,
        generation: int,
        unit_type: str,
        entity_type: str,
        entity_key: str,
        before: Mapping[str, Any] | None,
    ) -> str:
        from ..infrastructure.sql import ErpSyncMutationEvidence
        from ..infrastructure.sql.base import new_id

        existing = session.scalar(
            select(ErpSyncMutationEvidence).where(
                ErpSyncMutationEvidence.attempt_id == attempt_id,
                ErpSyncMutationEvidence.unit_type == unit_type,
                ErpSyncMutationEvidence.entity_type == entity_type,
                ErpSyncMutationEvidence.entity_key == entity_key,
            )
        )
        before_present = before is not None
        plain = canonical_json_bytes(before if before is not None else {"present": False})
        digest = hashlib.sha256(plain).hexdigest()
        if existing is not None:
            if (
                bool(existing.before_present) != before_present
                or existing.before_sha256 != digest
                or int(existing.generation) != int(generation)
            ):
                raise ApplicationConflictError(
                    "La preuve before-image ERP existante est incompatible.",
                    code="erp_sync_checkpoint_before_conflict",
                )
            return existing.id
        encrypted = self._encrypt_bytes(plain) if before_present else None
        if encrypted is not None:
            self._consume_bytes(
                session,
                attempt_id=attempt_id,
                amount=len(encrypted.encode("utf-8")),
            )
        row = ErpSyncMutationEvidence(
            id=new_id(),
            attempt_id=attempt_id,
            generation=int(generation),
            unit_type=unit_type,
            entity_type=entity_type,
            entity_key=entity_key,
            before_present=before_present,
            encrypted_before=encrypted,
            before_sha256=digest,
        )
        session.add(row)
        session.flush()
        return row.id

    def record_after_in_session(
        self,
        session: Session,
        *,
        evidence_id: str,
        attempt_id: str,
        generation: int,
        after: Mapping[str, Any],
    ) -> None:
        from ..infrastructure.sql import ErpSyncMutationEvidence

        row = session.get(ErpSyncMutationEvidence, evidence_id)
        if (
            row is None
            or row.attempt_id != attempt_id
            or int(row.generation) != int(generation)
        ):
            raise ApplicationConflictError(
                "La preuve de mutation ERP ne correspond plus à la tentative.",
                code="erp_sync_checkpoint_fence_mismatch",
            )
        plain = canonical_json_bytes(after)
        digest = hashlib.sha256(plain).hexdigest()
        encrypted = self._encrypt_bytes(plain)
        if row.after_sha256 is not None:
            if row.after_sha256 != digest:
                raise ApplicationConflictError(
                    "La preuve after-image ERP est déjà scellée avec un autre état.",
                    code="erp_sync_checkpoint_after_conflict",
                )
            return
        self._consume_bytes(
            session,
            attempt_id=attempt_id,
            amount=len(encrypted.encode("utf-8")),
        )
        row.encrypted_after = encrypted
        row.after_sha256 = digest
        row.committed_at = _utc_now()

    def mutation_payloads(
        self,
        *,
        attempt_id: str,
    ) -> tuple[tuple[Any, Mapping[str, Any] | None, Mapping[str, Any] | None], ...]:
        from ..infrastructure.sql import ErpSyncMutationEvidence

        with self._session_factory() as session:
            rows = tuple(
                session.scalars(
                    select(ErpSyncMutationEvidence)
                    .where(ErpSyncMutationEvidence.attempt_id == attempt_id)
                    .order_by(
                        ErpSyncMutationEvidence.unit_type,
                        ErpSyncMutationEvidence.entity_type,
                        ErpSyncMutationEvidence.entity_key,
                    )
                )
            )
            result = []
            for row in rows:
                before = None
                after = None
                if row.encrypted_before:
                    before = json.loads(
                        self._decrypt_bytes(row.encrypted_before).decode("utf-8")
                    )
                if row.encrypted_after:
                    after = json.loads(
                        self._decrypt_bytes(row.encrypted_after).decode("utf-8")
                    )
                result.append((row, before, after))
            return tuple(result)

    def mark_recovery_required(
        self,
        *,
        attempt_id: str,
        reason_code: str,
    ) -> None:
        from ..infrastructure.sql import (
            RECOVERY_REQUIRED,
            ErpSyncEvidenceManifest,
            ErpSyncRecoveryState,
        )
        from ..infrastructure.sql.session import transactional_session

        with transactional_session(self._session_factory) as session:
            row = session.scalar(
                select(ErpSyncRecoveryState).where(
                    ErpSyncRecoveryState.attempt_id == attempt_id
                )
            )
            if row is None:
                row = ErpSyncRecoveryState(
                    attempt_id=attempt_id,
                    state=RECOVERY_REQUIRED,
                    reason_code=str(reason_code or "erp_sync_recovery_required"),
                    pinned=True,
                )
                session.add(row)
            else:
                row.state = RECOVERY_REQUIRED
                row.reason_code = str(reason_code or "erp_sync_recovery_required")
                row.pinned = True
            for manifest in session.scalars(
                select(ErpSyncEvidenceManifest).where(
                    ErpSyncEvidenceManifest.attempt_id == attempt_id
                )
            ):
                manifest.pinned = True

    def list_attempt_sources(self, *, attempt_id: str) -> tuple[dict[str, Any], ...]:
        from ..infrastructure.sql import ErpSyncEvidenceManifest

        with self._session_factory() as session:
            rows = tuple(
                session.scalars(
                    select(ErpSyncEvidenceManifest)
                    .where(ErpSyncEvidenceManifest.attempt_id == attempt_id)
                    .order_by(
                        ErpSyncEvidenceManifest.source_type,
                        ErpSyncEvidenceManifest.proof_type,
                    )
                )
            )
            return tuple(
                {
                    "manifest_id": row.id,
                    "attempt_id": row.attempt_id,
                    "protocol_version": int(row.protocol_version),
                    "source_type": row.source_type,
                    "proof_type": row.proof_type,
                    "profile_fingerprint": row.profile_fingerprint,
                    "source_fingerprint": row.source_fingerprint,
                    "observed_at": row.observed_at,
                    "row_count": int(row.row_count),
                    "chunk_count": int(row.chunk_count),
                    "plaintext_bytes": int(row.plaintext_bytes),
                    "ciphertext_bytes": int(row.ciphertext_bytes),
                    "content_sha256": row.content_sha256,
                    "state": row.state,
                    "pinned": bool(row.pinned),
                    "expires_at": row.expires_at,
                    "diagnostic": row.diagnostic,
                }
                for row in rows
            )

    def get_baseline_summary(self, *, source_type: str, profile_fingerprint: str) -> dict[str, Any] | None:
        from ..infrastructure.sql import ErpSyncBaseline

        with self._session_factory() as session:
            row = session.scalar(
                select(ErpSyncBaseline).where(
                    ErpSyncBaseline.source_type == source_type,
                    ErpSyncBaseline.profile_fingerprint == profile_fingerprint,
                )
            )
            if row is None:
                return None
            return {
                "baseline_id": row.id,
                "source_type": row.source_type,
                "profile_fingerprint": row.profile_fingerprint,
                "source_fingerprint": row.source_fingerprint,
                "row_count": int(row.row_count),
                "approved_by": row.approved_by,
                "approval_reason": row.approval_reason,
                "approved_at": row.approved_at,
            }

    @staticmethod
    def _project_counts_from_payload(payload: Any) -> dict[str, int]:
        if not isinstance(payload, dict):
            return {}
        portfolio = payload.get("portfolio", payload)
        if not isinstance(portfolio, dict):
            return {}
        snapshots = portfolio.get("snapshots")
        if not isinstance(snapshots, list):
            return {}
        counts: dict[str, int] = {}
        for snapshot in snapshots:
            if not isinstance(snapshot, dict):
                continue
            number = str(snapshot.get("project_number") or "").strip()
            if number:
                counts[number] = int(snapshot.get("source_rows") or 0)
        return counts

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
        if project_counts is None:
            payload = self.load_source(manifest_id=manifest_id)
            project_counts = self._project_counts_from_payload(payload) or None
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

    def approve_plausibility_exception(
        self,
        *,
        attempt_id: str,
        source_type: str,
        source_fingerprint: str,
        threshold_scope: str,
        actor_name: str,
        reason: str,
    ) -> str:
        from ..infrastructure.sql import ErpSyncPlausibilityException
        from ..infrastructure.sql.base import new_id
        from ..infrastructure.sql.session import transactional_session

        scope = " ".join(str(threshold_scope or "").split())
        actor = " ".join(str(actor_name or "").split())
        if not scope or not actor:
            raise ApplicationValidationError(
                "Le périmètre et l'acteur de l'exception sont requis.",
                code="erp_sync_plausibility_exception_invalid",
            )
        reason_value = _safe_reason(reason)
        with transactional_session(self._session_factory) as session:
            existing = session.scalar(
                select(ErpSyncPlausibilityException).where(
                    ErpSyncPlausibilityException.attempt_id == attempt_id,
                    ErpSyncPlausibilityException.source_type == source_type,
                    ErpSyncPlausibilityException.source_fingerprint == source_fingerprint,
                    ErpSyncPlausibilityException.threshold_scope == scope,
                )
            )
            if existing is not None:
                return existing.id
            row = ErpSyncPlausibilityException(
                id=new_id(),
                attempt_id=attempt_id,
                source_type=source_type,
                source_fingerprint=source_fingerprint,
                threshold_scope=scope,
                actor_name=actor,
                reason=reason_value,
                expires_at=_utc_now() + timedelta(days=1),
            )
            session.add(row)
            session.flush()
            return row.id

    def advance_approved_baselines(self, *, attempt_id: str) -> None:
        """Advance only already-human-approved comparable baselines after full success."""
        from ..infrastructure.sql import (
            EVIDENCE_PROOF_SOURCE,
            EVIDENCE_SOURCE_PROJECTS,
            EVIDENCE_SOURCE_TASKS,
            EVIDENCE_STATE_SEALED,
            ErpSyncBaseline,
            ErpSyncEvidenceManifest,
        )
        from ..infrastructure.sql.session import transactional_session

        with self._session_factory() as session:
            manifests = tuple(
                session.scalars(
                    select(ErpSyncEvidenceManifest).where(
                        ErpSyncEvidenceManifest.attempt_id == attempt_id,
                        ErpSyncEvidenceManifest.proof_type == EVIDENCE_PROOF_SOURCE,
                        ErpSyncEvidenceManifest.state == EVIDENCE_STATE_SEALED,
                        ErpSyncEvidenceManifest.source_type.in_(
                            (EVIDENCE_SOURCE_PROJECTS, EVIDENCE_SOURCE_TASKS)
                        ),
                    )
                )
            )
            manifest_meta = tuple(
                (
                    row.id,
                    row.source_type,
                    row.profile_fingerprint,
                    row.source_fingerprint,
                    int(row.row_count),
                )
                for row in manifests
            )
        if len(manifest_meta) != 2:
            raise ApplicationConflictError(
                "Les deux preuves source scellées sont requises pour publier le baseline.",
                code="erp_sync_baseline_publication_incomplete",
            )
        task_counts_by_manifest = {
            manifest_id: self._project_counts_from_payload(
                self.load_source(manifest_id=manifest_id)
            )
            for manifest_id, source_type, *_rest in manifest_meta
            if source_type == EVIDENCE_SOURCE_TASKS
        }
        with transactional_session(self._session_factory) as session:
            for (
                manifest_id,
                source_type,
                profile_fingerprint,
                source_fingerprint,
                row_count,
            ) in manifest_meta:
                baseline = session.scalar(
                    select(ErpSyncBaseline).where(
                        ErpSyncBaseline.source_type == source_type,
                        ErpSyncBaseline.profile_fingerprint == profile_fingerprint,
                    )
                )
                if baseline is None:
                    raise ApplicationConflictError(
                        "Un baseline humain approuvé est requis avant publication automatique.",
                        code="erp_sync_baseline_required",
                    )
                baseline.source_fingerprint = str(source_fingerprint)
                baseline.row_count = row_count
                if source_type == EVIDENCE_SOURCE_TASKS:
                    counts = task_counts_by_manifest.get(manifest_id, {})
                    baseline.encrypted_project_counts = (
                        self._encrypt_bytes(canonical_json_bytes(counts))
                        if counts
                        else None
                    )

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
            if diagnostics:
                from ..infrastructure.sql import ErpSyncPlausibilityException

                scopes = {
                    "GLOBAL"
                    if not diagnostic.startswith("erp_sync_project_ratio_too_low:")
                    else diagnostic.split(":", 1)[1]
                    for diagnostic in diagnostics
                }
                now = _utc_now()
                approved = {
                    str(row.threshold_scope)
                    for row in session.scalars(
                        select(ErpSyncPlausibilityException).where(
                            ErpSyncPlausibilityException.attempt_id
                            == self._manifest_attempt(session, captured.manifest_id),
                            ErpSyncPlausibilityException.source_type
                            == captured.source_type,
                            ErpSyncPlausibilityException.source_fingerprint
                            == captured.source_fingerprint,
                            ErpSyncPlausibilityException.expires_at > now,
                        )
                    )
                }
                diagnostics = [
                    diagnostic
                    for diagnostic in diagnostics
                    if (
                        "GLOBAL"
                        if not diagnostic.startswith("erp_sync_project_ratio_too_low:")
                        else diagnostic.split(":", 1)[1]
                    )
                    not in approved
                ]
            return ErpSyncPlausibilityResult(
                accepted=not diagnostics,
                diagnostics=tuple(diagnostics),
            )

    @staticmethod
    def _manifest_attempt(session: Session, manifest_id: str) -> str:
        from ..infrastructure.sql import ErpSyncEvidenceManifest

        manifest = session.get(ErpSyncEvidenceManifest, manifest_id)
        if manifest is None:
            raise ApplicationValidationError(
                "La preuve ERP est introuvable.",
                code="erp_sync_evidence_not_found",
            )
        return str(manifest.attempt_id)

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

    def assert_no_required_recovery(self) -> None:
        from ..infrastructure.sql import RECOVERY_REQUIRED, ErpSyncRecoveryState

        with self._session_factory() as session:
            blocking = session.scalar(
                select(ErpSyncRecoveryState.id)
                .where(ErpSyncRecoveryState.state == RECOVERY_REQUIRED)
                .limit(1)
            )
            if blocking is not None:
                raise ApplicationConflictError(
                    "Une récupération ERP requise bloque un nouvel import.",
                    code="erp_sync_recovery_required",
                )

    def finalize_attempt(
        self,
        *,
        attempt_id: str,
        successful: bool,
    ) -> None:
        from ..infrastructure.sql import (
            RECOVERY_REQUIRED,
            ErpSyncEvidenceManifest,
            ErpSyncEvidenceReservation,
            ErpSyncRecoveryState,
        )
        from ..infrastructure.sql.session import transactional_session

        now = _utc_now()
        retention_days = (
            self.settings.history_days
            if successful
            else self.settings.failure_retention_days
        )
        with transactional_session(self._session_factory) as session:
            recovery = session.scalar(
                select(ErpSyncRecoveryState).where(
                    ErpSyncRecoveryState.attempt_id == attempt_id
                )
            )
            pin = recovery is not None and recovery.state == RECOVERY_REQUIRED
            for manifest in session.scalars(
                select(ErpSyncEvidenceManifest).where(
                    ErpSyncEvidenceManifest.attempt_id == attempt_id
                )
            ):
                manifest.pinned = bool(manifest.pinned or pin)
                if not manifest.pinned:
                    manifest.expires_at = now + timedelta(days=retention_days)
            reservation = session.scalar(
                select(ErpSyncEvidenceReservation).where(
                    ErpSyncEvidenceReservation.attempt_id == attempt_id
                )
            )
            if reservation is not None and reservation.released_at is None:
                reservation.released_at = now

    def automatic_execution_available(self, *, profile_fingerprint: str) -> bool:
        if self._cipher is None or self._control_settings is None:
            return False
        from ..infrastructure.sql import ErpSyncBaseline

        try:
            probe = b'{"probe":"erp-sync-643b"}'
            if self._decrypt_bytes(self._encrypt_bytes(probe)) != probe:
                return False
            with self._session_factory() as session:
                inspector = inspect(session.get_bind())
                required_tables = {
                    "erp_sync_control",
                    "erp_sync_attempts",
                    "erp_sync_evidence_manifests",
                    "erp_sync_evidence_chunks",
                    "erp_sync_evidence_reservations",
                    "erp_sync_baselines",
                    "erp_sync_mutation_evidence",
                    "erp_sync_recovery_state",
                }
                if not all(inspector.has_table(name) for name in required_tables):
                    return False
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
        except Exception:
            return False

    @staticmethod
    def _preview_hash(attempt_id: str, rows: Sequence[tuple[Any, Mapping[str, Any] | None, Mapping[str, Any] | None]]) -> str:
        material = [
            {
                "attempt_id": attempt_id,
                "evidence_id": row.id,
                "entity_type": row.entity_type,
                "entity_key": row.entity_key,
                "before_sha256": row.before_sha256,
                "after_sha256": row.after_sha256,
            }
            for row, _before, _after in rows
            if row.committed_at is not None and row.after_sha256 is not None
        ]
        return content_sha256(material)

    def _current_owned_state(self, session: Session, row: Any) -> Mapping[str, Any] | None:
        from ..infrastructure.sql import Project, TaskCatalogEntry, TaskCatalogProjectSyncState

        if row.entity_type == "PROJECT":
            project = session.scalar(
                select(Project).where(
                    (Project.erp_external_id == row.entity_key)
                    | (Project.number == row.entity_key)
                )
            )
            if project is None:
                return None
            return {
                "erp_external_id": project.erp_external_id,
                "number": project.number,
                "name": project.name,
                "client": project.client,
                "project_manager_external_id": project.project_manager_external_id,
                "project_manager_name": project.project_manager_name,
                "status": project.status,
            }
        if row.entity_type == "TASK":
            if ":" in row.entity_key:
                project_number, task_code = row.entity_key.split(":", 1)
                task = session.scalar(
                    select(TaskCatalogEntry).where(
                        TaskCatalogEntry.project_number == project_number,
                        TaskCatalogEntry.task_code == task_code,
                    )
                )
            else:
                task = session.scalar(
                    select(TaskCatalogEntry).where(
                        TaskCatalogEntry.erp_task_id == row.entity_key
                    )
                )
            if task is None:
                return None
            return {
                "project_number": task.project_number,
                "task_code": task.task_code,
                "label": task.label,
                "status": task.status,
                "active": bool(task.active),
                "billing_rule": task.billing_rule,
                "allocation_rule": task.allocation_rule,
                "completion_percent": task.completion_percent,
                "erp_created_at": task.erp_created_at,
                "branch": task.branch,
                "approver_name": task.approver_name,
                "cv_enabled": task.cv_enabled,
                "time_entry_enabled": task.time_entry_enabled,
                "expenses_enabled": task.expenses_enabled,
                "erp_task_id": task.erp_task_id,
                "account_group": task.account_group,
                "cost_code": task.cost_code,
                "inventory_id": task.inventory_id,
                "budget_amount_cad": task.budget_amount_cad,
                "budget_actual_cad": task.budget_actual_cad,
                "budget_diagnostic": task.budget_diagnostic,
                "workforce_eligible": task.workforce_eligible,
                "resource_class_code": task.resource_class_code,
                "average_hourly_cost_cad": task.average_hourly_cost_cad,
                "budget_hours": task.budget_hours,
                "workforce_diagnostics": task.workforce_diagnostics,
                "erp_budget_last_success_at": task.erp_budget_last_success_at,
            }
        if row.entity_type == "TASK_SYNC_STATE":
            state = session.get(TaskCatalogProjectSyncState, row.entity_key)
            if state is None:
                return None
            return {
                "project_number": state.project_number,
                "last_attempt_at": state.last_attempt_at,
                "last_success_at": state.last_success_at,
                "source_rows": state.source_rows,
                "task_count": state.task_count,
                "rejected_rows": state.rejected_rows,
                "duration_ms": state.duration_ms,
                "last_error_code": state.last_error_code,
            }
        return None

    def compensation_preview(self, *, attempt_id: str) -> dict[str, Any]:
        rows = self.mutation_payloads(attempt_id=attempt_id)
        if not rows:
            raise ApplicationValidationError(
                "Aucune preuve de mutation n'est disponible pour cette tentative.",
                code="erp_sync_compensation_evidence_missing",
            )
        conflicts: list[str] = []
        eligible = 0
        with self._session_factory() as session:
            for row, _before, after in rows:
                if row.committed_at is None or row.after_sha256 is None or after is None:
                    continue
                eligible += 1
                current = self._current_owned_state(session, row)
                if current is None or content_sha256(current) != row.after_sha256:
                    conflicts.append(f"{row.entity_type}:{row.entity_key}")
        preview_hash = self._preview_hash(attempt_id, rows)
        return {
            "attempt_id": attempt_id,
            "preview_hash": preview_hash,
            "eligible_records": eligible,
            "conflict_count": len(conflicts),
            "can_confirm": eligible > 0 and not conflicts,
            "conflicts": tuple(conflicts[:50]),
        }

    def confirm_compensation(
        self,
        *,
        attempt_id: str,
        preview_hash: str,
        command_identity: str,
        actor_name: str,
        reason: str,
    ) -> dict[str, Any]:
        from ..infrastructure.sql import (
            RECOVERY_RESOLVED,
            ErpSyncCompensationReceipt,
            ErpSyncMutationEvidence,
            ErpSyncRecoveryState,
            Project,
            TaskCatalogEntry,
            TaskCatalogProjectSyncState,
        )
        from ..infrastructure.sql.base import new_id
        from ..infrastructure.sql.session import transactional_session

        reason_value = _safe_reason(reason)
        identity = " ".join(str(command_identity or "").split())
        if not identity:
            raise ApplicationValidationError(
                "L'identité idempotente de compensation est requise.",
                code="erp_sync_compensation_identity_required",
            )
        with transactional_session(self._session_factory) as session:
            if self._control_settings is None:
                raise ApplicationUnavailableError(
                    "Le profil de contrôle ERP requis pour la compensation est absent.",
                    code="erp_sync_recovery_control_unavailable",
                )
            from ..infrastructure.sql.erp_sync_control_repository import (
                SqlErpSyncControlRepository,
            )
            SqlErpSyncControlRepository.guard_recovery_session(
                session,
                settings=self._control_settings,
            )
            existing = session.scalar(
                select(ErpSyncCompensationReceipt).where(
                    ErpSyncCompensationReceipt.command_identity == identity
                )
            )
            if existing is not None:
                return {
                    "receipt_id": existing.id,
                    "status": existing.status,
                    "affected_records": int(existing.affected_records),
                }
            rows = tuple(
                session.scalars(
                    select(ErpSyncMutationEvidence)
                    .where(
                        ErpSyncMutationEvidence.attempt_id == attempt_id,
                        ErpSyncMutationEvidence.committed_at.is_not(None),
                        ErpSyncMutationEvidence.after_sha256.is_not(None),
                    )
                    .order_by(
                        ErpSyncMutationEvidence.unit_type.desc(),
                        ErpSyncMutationEvidence.entity_type.desc(),
                    )
                )
            )
            payload_rows = []
            for row in rows:
                before = (
                    json.loads(self._decrypt_bytes(row.encrypted_before).decode("utf-8"))
                    if row.encrypted_before
                    else None
                )
                after = (
                    json.loads(self._decrypt_bytes(row.encrypted_after).decode("utf-8"))
                    if row.encrypted_after
                    else None
                )
                payload_rows.append((row, before, after))
            if self._preview_hash(attempt_id, payload_rows) != str(preview_hash):
                raise ApplicationConflictError(
                    "La prévisualisation de compensation est périmée.",
                    code="erp_sync_compensation_preview_stale",
                )
            affected = 0
            for row, before, _after in payload_rows:
                current = self._current_owned_state(session, row)
                if current is None or content_sha256(current) != row.after_sha256:
                    raise ApplicationConflictError(
                        "L'état courant ne correspond plus au checkpoint.",
                        code="erp_sync_compensation_current_state_changed",
                        context={"entity_type": row.entity_type},
                    )
                if row.entity_type == "PROJECT":
                    project = session.scalar(
                        select(Project).where(
                            (Project.erp_external_id == row.entity_key)
                            | (Project.number == row.entity_key)
                        )
                    )
                    if project is None:
                        raise ApplicationConflictError(
                            "Le projet checkpointé n'existe plus.",
                            code="erp_sync_compensation_current_state_changed",
                        )
                    if before is None:
                        # Never cascade-delete a newly imported project. Remove ERP
                        # authority and mark it inactive while preserving all local
                        # references/authority that may now depend on the row.
                        project.erp_external_id = None
                        project.status = "Annulé"
                    else:
                        for field in (
                            "erp_external_id",
                            "number",
                            "name",
                            "client",
                            "project_manager_external_id",
                            "project_manager_name",
                            "status",
                        ):
                            setattr(project, field, before.get(field))
                    affected += 1
                elif row.entity_type == "TASK":
                    if ":" in row.entity_key:
                        project_number, task_code = row.entity_key.split(":", 1)
                        task = session.scalar(
                            select(TaskCatalogEntry).where(
                                TaskCatalogEntry.project_number == project_number,
                                TaskCatalogEntry.task_code == task_code,
                            )
                        )
                    else:
                        task = session.scalar(
                            select(TaskCatalogEntry).where(
                                TaskCatalogEntry.erp_task_id == row.entity_key
                            )
                        )
                    if task is None:
                        raise ApplicationConflictError(
                            "La tâche checkpointée n'existe plus.",
                            code="erp_sync_compensation_current_state_changed",
                        )
                    if before is None:
                        # Preserve local foreign keys; revoke ERP freshness/identity
                        # rather than destructively deleting a newly created row.
                        task.active = False
                        task.erp_task_id = None
                        task.erp_budget_last_success_at = None
                    else:
                        for field, value in before.items():
                            if field in {
                                "preferred_resource_id",
                                "preferred_resource_version",
                                "operational_responsible_contact_id",
                                "coordinator_contact_id",
                            }:
                                continue
                            setattr(task, field, value)
                    affected += 1
                elif row.entity_type == "TASK_SYNC_STATE":
                    state = session.get(TaskCatalogProjectSyncState, row.entity_key)
                    if before is None:
                        if state is not None:
                            session.delete(state)
                    else:
                        if state is None:
                            state = TaskCatalogProjectSyncState(**before)
                            session.add(state)
                        else:
                            for field, value in before.items():
                                setattr(state, field, value)
                    affected += 1
            recovery = session.scalar(
                select(ErpSyncRecoveryState).where(
                    ErpSyncRecoveryState.attempt_id == attempt_id
                )
            )
            if recovery is not None:
                recovery.state = RECOVERY_RESOLVED
                recovery.pinned = False
                recovery.resolved_by = actor_name
                recovery.resolved_at = _utc_now()
            receipt = ErpSyncCompensationReceipt(
                id=new_id(),
                attempt_id=attempt_id,
                command_identity=identity,
                preview_hash=str(preview_hash),
                actor_name=str(actor_name),
                reason=reason_value,
                status="COMPLETED",
                affected_records=affected,
            )
            session.add(receipt)
            session.flush()
            return {
                "receipt_id": receipt.id,
                "status": receipt.status,
                "affected_records": affected,
            }

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

        now = _utc_now()
        purged = 0
        with transactional_session(self._session_factory) as session:
            rows = tuple(
                session.scalars(
                    select(ErpSyncEvidenceManifest).where(
                        ErpSyncEvidenceManifest.pinned.is_(False),
                        ErpSyncEvidenceManifest.expires_at.is_not(None),
                        ErpSyncEvidenceManifest.expires_at <= now,
                        ErpSyncEvidenceManifest.state.not_in((EVIDENCE_STATE_PURGED,)),
                    )
                )
            )
            affected_attempts: set[str] = set()
            for row in rows:
                row.state = EVIDENCE_STATE_EXPIRED
                session.query(ErpSyncEvidenceChunk).filter(
                    ErpSyncEvidenceChunk.manifest_id == row.id
                ).delete(synchronize_session=False)
                row.state = EVIDENCE_STATE_PURGED
                row.diagnostic = "erp_sync_evidence_retention_purged"
                affected_attempts.add(str(row.attempt_id))
                purged += 1

            if affected_attempts:
                from ..infrastructure.sql import (
                    RECOVERY_REQUIRED,
                    ErpSyncEvidenceReservation,
                    ErpSyncMutationEvidence,
                    ErpSyncRecoveryState,
                )
                for attempt_id in affected_attempts:
                    remaining = int(
                        session.scalar(
                            select(func.count(ErpSyncEvidenceManifest.id)).where(
                                ErpSyncEvidenceManifest.attempt_id == attempt_id,
                                ErpSyncEvidenceManifest.state != EVIDENCE_STATE_PURGED,
                            )
                        )
                        or 0
                    )
                    recovery = session.scalar(
                        select(ErpSyncRecoveryState).where(
                            ErpSyncRecoveryState.attempt_id == attempt_id
                        )
                    )
                    if (
                        remaining == 0
                        and not (
                            recovery is not None
                            and recovery.state == RECOVERY_REQUIRED
                        )
                    ):
                        session.query(ErpSyncMutationEvidence).filter(
                            ErpSyncMutationEvidence.attempt_id == attempt_id
                        ).delete(synchronize_session=False)
                        reservation = session.scalar(
                            select(ErpSyncEvidenceReservation).where(
                                ErpSyncEvidenceReservation.attempt_id == attempt_id
                            )
                        )
                        if reservation is not None:
                            reservation.used_bytes = 0
        return purged
