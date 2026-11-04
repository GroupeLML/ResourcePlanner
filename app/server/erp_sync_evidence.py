from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
import base64
from decimal import Decimal
import hashlib
import json
from typing import Any, Mapping, Sequence

from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from ..application.errors import (
    ApplicationConflictError,
    ApplicationUnavailableError,
    ApplicationValidationError,
)
from ..application.erp_sync_scheduler import (
    ERP_SYNC_PROTOCOL_VERSION,
    ErpSyncSchedulerSettings,
)
from ..application.smtp_settings import SecretCipherPort


ERP_EVIDENCE_CHUNK_BYTES = 512 * 1024
ERP_EVIDENCE_CODEC_VERSION = 1


def _typed_value(value: Any) -> Any:
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, Mapping):
        return {
            str(key): _typed_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_typed_value(item) for item in value]
    if isinstance(value, Decimal):
        return {"$erp_type": "decimal", "value": format(value, "f")}
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return {
            "$erp_type": "datetime",
            "value": value.astimezone(timezone.utc).isoformat(),
        }
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return {"$erp_type": "string", "value": str(value)}


def _from_typed_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_from_typed_value(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {"$erp_type", "value"}:
            kind = value.get("$erp_type")
            raw = value.get("value")
            if kind == "decimal":
                return Decimal(str(raw))
            if kind == "datetime":
                parsed = datetime.fromisoformat(str(raw))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed
            if kind == "string":
                return str(raw)
            raise ApplicationConflictError(
                "La version typée de la preuve ERP est incompatible.",
                code="erp_sync_evidence_codec_incompatible",
            )
        return {str(key): _from_typed_value(item) for key, item in value.items()}
    return value


def canonical_json_bytes(value: Any) -> bytes:
    envelope = {
        "codec_version": ERP_EVIDENCE_CODEC_VERSION,
        "payload": _typed_value(value),
    }
    return json.dumps(
        envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def decode_canonical_json_bytes(payload: bytes) -> Any:
    try:
        envelope = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApplicationConflictError(
            "Le contenu normalisé de la preuve ERP est invalide.",
            code="erp_sync_evidence_payload_invalid",
        ) from exc
    if not isinstance(envelope, dict) or int(envelope.get("codec_version") or 0) != ERP_EVIDENCE_CODEC_VERSION:
        raise ApplicationConflictError(
            "La version du codec de preuve ERP est incompatible.",
            code="erp_sync_evidence_codec_incompatible",
        )
    return _from_typed_value(envelope.get("payload"))


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
    rejected_projects: tuple[str, ...] = ()
    reviewable: bool = False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalized_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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

    def policy_fingerprint_in_session(
        self,
        session: Session,
        *,
        lock_for_use: bool = False,
    ) -> str:
        from ..infrastructure.sql import (
            ProjectTaskClassOverride,
            ResourceClassConfig,
            TaskClassStandard,
        )

        def _policy_rows(model, *order_by):
            statement = select(model).order_by(*order_by)
            if lock_for_use and session.get_bind().dialect.name == "mssql":
                statement = statement.with_hint(
                    model,
                    "WITH (UPDLOCK, HOLDLOCK)",
                    dialect_name="mssql",
                )
            return tuple(session.scalars(statement))

        resource_classes = _policy_rows(
            ResourceClassConfig,
            ResourceClassConfig.code,
        )
        standards = _policy_rows(
            TaskClassStandard,
            TaskClassStandard.task_code,
        )
        overrides = _policy_rows(
            ProjectTaskClassOverride,
            ProjectTaskClassOverride.project_id,
            ProjectTaskClassOverride.task_code,
        )
        material = {
            "contract": "ERP_SYNC_643B_POLICY_V2",
            "control_profile": (
                self._control_settings.config_fingerprint
                if self._control_settings is not None
                else None
            ),
            "evidence_policy": {
                "min_source_ratio": self.settings.min_source_ratio,
                "min_projects": self.settings.min_projects,
                "min_task_rows": self.settings.min_task_rows,
                "max_evidence_mib": self.settings.max_evidence_mib,
                "evidence_budget_mib": self.settings.evidence_budget_mib,
                "failure_retention_days": self.settings.failure_retention_days,
                "history_days": self.settings.history_days,
            },
            "resource_classes": [
                {
                    "code": row.code,
                    "average_hourly_cost_cad": row.average_hourly_cost_cad,
                    "active": bool(row.active),
                    "version": int(row.version),
                }
                for row in resource_classes
            ],
            "task_standards": [
                {
                    "task_code": row.task_code,
                    "resource_class_code": row.resource_class_code,
                    "active": bool(row.active),
                    "version": int(row.version),
                }
                for row in standards
            ],
            "project_overrides": [
                {
                    "project_id": row.project_id,
                    "task_code": row.task_code,
                    "resource_class_code": row.resource_class_code,
                    "excluded": bool(row.excluded),
                    "version": int(row.version),
                }
                for row in overrides
            ],
        }
        return content_sha256(material)

    def bind_attempt_policy(
        self,
        *,
        attempt_id: str,
        generation: int,
    ) -> str:
        from ..infrastructure.sql import ErpSyncAttempt
        from ..infrastructure.sql.erp_sync_control_repository import (
            SqlErpSyncControlRepository,
        )
        from ..infrastructure.sql.session import transactional_session

        if self._control_settings is None:
            raise ApplicationUnavailableError(
                "Le profil de contrôle ERP est absent.",
                code="erp_sync_evidence_control_unavailable",
            )
        with transactional_session(self._session_factory) as session:
            SqlErpSyncControlRepository.guard_session(
                session,
                attempt_id=attempt_id,
                generation=generation,
                settings=self._control_settings,
            )
            attempt = session.get(ErpSyncAttempt, attempt_id)
            if attempt is None:
                raise ApplicationValidationError(
                    "La tentative ERP est introuvable.",
                    code="erp_sync_attempt_not_found",
                )
            fingerprint = self.policy_fingerprint_in_session(
                session,
                lock_for_use=True,
            )
            if attempt.policy_fingerprint is None:
                attempt.policy_fingerprint = fingerprint
            elif attempt.policy_fingerprint != fingerprint:
                raise ApplicationConflictError(
                    "La politique de référence ERP a changé pendant la tentative.",
                    code="erp_sync_policy_fingerprint_changed",
                )
            return fingerprint

    def assert_attempt_policy_in_session(
        self,
        session: Session,
        *,
        attempt_id: str,
    ) -> str:
        from ..infrastructure.sql import ErpSyncAttempt

        attempt = session.get(ErpSyncAttempt, attempt_id)
        if attempt is None or not attempt.policy_fingerprint:
            raise ApplicationConflictError(
                "La politique de référence ERP n'est pas figée pour cette tentative.",
                code="erp_sync_policy_fingerprint_missing",
            )
        current = self.policy_fingerprint_in_session(
            session,
            lock_for_use=True,
        )
        if str(attempt.policy_fingerprint) != current:
            raise ApplicationConflictError(
                "La politique de référence ERP a changé pendant la tentative.",
                code="erp_sync_policy_fingerprint_changed",
            )
        return current

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
        encoded = base64.b64encode(payload).decode("ascii")
        return cipher.encrypt(encoded)

    def _decrypt_bytes(self, encrypted: str) -> bytes:
        cipher = self._require_cipher()
        try:
            encoded = cipher.decrypt(encrypted)
            return base64.b64decode(encoded.encode("ascii"), validate=True)
        except Exception as exc:
            raise ApplicationConflictError(
                "Une preuve ERP ne peut pas être déchiffrée.",
                code="erp_sync_evidence_decrypt_failed",
            ) from exc

    @staticmethod
    def _encrypted_storage_bound(plaintext_bytes: int) -> int:
        """Conservative bound for base64 + Fernet storage at source chunk size."""
        remaining = max(0, int(plaintext_bytes))
        total = 1024  # checkpoint/accounting manifest allowance
        while remaining:
            chunk = min(remaining, ERP_EVIDENCE_CHUNK_BYTES)
            encoded = 4 * ((chunk + 2) // 3)
            # Fernet encrypts the encoded text with PKCS7/AES-CBC, then wraps
            # version/timestamp/IV/ciphertext/HMAC in URL-safe base64.
            padded = 16 * ((encoded // 16) + 1)
            fernet_raw = 57 + padded
            total += 4 * ((fernet_raw + 2) // 3)
            remaining -= chunk
        return total

    def reserve_attempt(self, *, attempt_id: str) -> None:
        from ..infrastructure.sql import ErpSyncAttempt, ErpSyncEvidenceReservation
        from ..infrastructure.sql.erp_sync_control_repository import (
            SqlErpSyncControlRepository,
        )
        from ..infrastructure.sql.session import transactional_session

        requested_plaintext = self.settings.max_evidence_bytes
        # Reserve from the actual codec/encryption envelope rather than a 1.5x
        # heuristic. This covers the configured maximum source payload after the
        # outer base64 step, Fernet framing/AES padding, and chunking overhead.
        requested_storage = self._encrypted_storage_bound(requested_plaintext)
        if requested_storage > self.settings.evidence_budget_bytes:
            raise ApplicationUnavailableError(
                "Le budget de stockage des preuves ERP ne couvre pas le plafond configuré.",
                code="erp_sync_evidence_storage_budget_unavailable",
            )
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
            fingerprint = self.policy_fingerprint_in_session(
                session,
                lock_for_use=True,
            )
            if attempt.policy_fingerprint is None:
                attempt.policy_fingerprint = fingerprint
            elif str(attempt.policy_fingerprint) != fingerprint:
                raise ApplicationConflictError(
                    "La politique de référence ERP a changé pendant la tentative.",
                    code="erp_sync_policy_fingerprint_changed",
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
            if (
                retained_used + active_reserved + requested_storage
                > self.settings.evidence_budget_bytes
            ):
                raise ApplicationUnavailableError(
                    "Le budget de stockage des preuves ERP est insuffisant.",
                    code="erp_sync_evidence_storage_budget_unavailable",
                )
            reservation = ErpSyncEvidenceReservation(
                attempt_id=attempt_id,
                reserved_bytes=requested_storage,
                used_bytes=1024,
                reserved_plaintext_bytes=requested_plaintext,
                used_plaintext_bytes=0,
            )
            session.add(reservation)
            from ..infrastructure.sql import (
                EVIDENCE_PROOF_CHECKPOINT,
                EVIDENCE_SOURCE_CHECKPOINT,
                EVIDENCE_STATE_READY,
                ERP_EVIDENCE_PROTOCOL_VERSION,
                ErpSyncEvidenceManifest,
            )
            session.add(
                ErpSyncEvidenceManifest(
                    attempt_id=attempt_id,
                    protocol_version=ERP_EVIDENCE_PROTOCOL_VERSION,
                    source_type=EVIDENCE_SOURCE_CHECKPOINT,
                    proof_type=EVIDENCE_PROOF_CHECKPOINT,
                    profile_fingerprint=fingerprint,
                    source_fingerprint=None,
                    observed_at=_utc_now(),
                    row_count=0,
                    chunk_count=0,
                    plaintext_bytes=0,
                    ciphertext_bytes=0,
                    content_sha256=None,
                    state=EVIDENCE_STATE_READY,
                    expires_at=_utc_now()
                    + timedelta(days=self.settings.failure_retention_days),
                )
            )

    def _consume_bytes(
        self,
        session: Session,
        *,
        attempt_id: str,
        plaintext_amount: int,
        storage_amount: int,
    ) -> None:
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
        next_plaintext = int(reservation.used_plaintext_bytes) + max(
            0, int(plaintext_amount)
        )
        if next_plaintext > int(reservation.reserved_plaintext_bytes):
            raise ApplicationUnavailableError(
                "La tentative ERP dépasse son plafond de preuve déchiffrée.",
                code="erp_sync_evidence_attempt_budget_exceeded",
            )
        next_storage = int(reservation.used_bytes) + max(0, int(storage_amount))
        if next_storage > int(reservation.reserved_bytes):
            raise ApplicationUnavailableError(
                "La tentative ERP dépasse son enveloppe de stockage réservée.",
                code="erp_sync_evidence_storage_reservation_exceeded",
            )
        reservation.used_plaintext_bytes = next_plaintext
        reservation.used_bytes = next_storage


    def capture_source(
        self,
        *,
        attempt_id: str,
        source_type: str,
        profile_fingerprint: str,
        payload: Any,
        row_count: int,
        project_counts: Mapping[str, int] | None = None,
        generation: int | None = None,
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
            from ..infrastructure.sql import ErpSyncAttempt
            from ..infrastructure.sql.erp_sync_control_repository import (
                SqlErpSyncControlRepository,
            )

            if self._control_settings is None:
                raise ApplicationUnavailableError(
                    "Le profil de contrôle ERP requis pour la preuve est absent.",
                    code="erp_sync_evidence_control_unavailable",
                )
            attempt = session.get(ErpSyncAttempt, attempt_id)
            if attempt is None:
                raise ApplicationValidationError(
                    "La tentative ERP est introuvable.",
                    code="erp_sync_attempt_not_found",
                )
            expected_generation = (
                int(generation)
                if generation is not None
                else int(attempt.generation)
            )
            SqlErpSyncControlRepository.guard_session(
                session,
                attempt_id=attempt_id,
                generation=expected_generation,
                settings=self._control_settings,
            )
            current_policy = self.assert_attempt_policy_in_session(
                session,
                attempt_id=attempt_id,
            )
            profile_fingerprint = current_policy
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
                plaintext_amount=len(normalized),
                storage_amount=ciphertext_total + 1024,
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
                expires_at=observed_at
                + timedelta(days=self.settings.failure_retention_days),
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

    def load_source_in_session(
        self,
        session: Session,
        *,
        manifest_id: str,
    ) -> Any:
        from ..infrastructure.sql import (
            EVIDENCE_STATE_PURGED,
            ERP_EVIDENCE_PROTOCOL_VERSION,
            ErpSyncEvidenceChunk,
            ErpSyncEvidenceManifest,
        )

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
        payload = b"".join(
            self._decrypt_bytes(row.encrypted_payload)
            for row in chunks
        )
        if hashlib.sha256(payload).hexdigest() != manifest.content_sha256:
            raise ApplicationConflictError(
                "L'intégrité de la preuve ERP est invalide.",
                code="erp_sync_evidence_integrity_invalid",
            )
        return decode_canonical_json_bytes(payload)

    def load_source(self, *, manifest_id: str) -> Any:
        with self._session_factory() as session:
            return self.load_source_in_session(
                session,
                manifest_id=manifest_id,
            )

    def load_captured_source(
        self,
        *,
        attempt_id: str,
        source_type: str,
    ) -> ErpSyncCapturedSource:
        from ..infrastructure.sql import (
            EVIDENCE_PROOF_SOURCE,
            ErpSyncEvidenceManifest,
        )

        with self._session_factory() as session:
            manifest = session.scalar(
                select(ErpSyncEvidenceManifest).where(
                    ErpSyncEvidenceManifest.attempt_id == attempt_id,
                    ErpSyncEvidenceManifest.source_type == source_type,
                    ErpSyncEvidenceManifest.proof_type == EVIDENCE_PROOF_SOURCE,
                )
            )
            if manifest is None or not manifest.source_fingerprint:
                raise ApplicationConflictError(
                    "La source ERP scellée requise est introuvable.",
                    code="erp_sync_evidence_source_missing",
                )
            manifest_id = str(manifest.id)
            row_count = int(manifest.row_count)
            fingerprint = str(manifest.source_fingerprint)
        payload = self.load_source(manifest_id=manifest_id)
        project_counts: dict[str, int] = {}
        if source_type == "TASKS" and isinstance(payload, dict):
            portfolio = payload.get("portfolio")
            if isinstance(portfolio, dict):
                for snapshot in portfolio.get("snapshots") or ():
                    if isinstance(snapshot, dict):
                        number = str(snapshot.get("project_number") or "").strip()
                        if number:
                            project_counts[number] = int(
                                snapshot.get("source_rows") or 0
                            )
        return ErpSyncCapturedSource(
            source_type=source_type,
            manifest_id=manifest_id,
            source_fingerprint=fingerprint,
            row_count=row_count,
            project_counts=project_counts,
            payload=payload,
        )

    def mark_sources_ready(
        self,
        *,
        attempt_id: str,
        generation: int | None = None,
    ) -> None:
        from ..infrastructure.sql import (
            EVIDENCE_PROOF_SOURCE,
            EVIDENCE_SOURCE_PROJECTS,
            EVIDENCE_SOURCE_TASKS,
            EVIDENCE_STATE_READY,
            EVIDENCE_STATE_SEALED,
            ErpSyncEvidenceManifest,
        )
        from ..infrastructure.sql.session import transactional_session

        with transactional_session(self._session_factory) as session:
            from ..infrastructure.sql import ErpSyncAttempt
            from ..infrastructure.sql.erp_sync_control_repository import (
                SqlErpSyncControlRepository,
            )

            if self._control_settings is None:
                raise ApplicationUnavailableError(
                    "Le profil de contrôle ERP requis pour publier READY est absent.",
                    code="erp_sync_evidence_control_unavailable",
                )
            attempt = session.get(ErpSyncAttempt, attempt_id)
            if attempt is None:
                raise ApplicationValidationError(
                    "La tentative ERP est introuvable.",
                    code="erp_sync_attempt_not_found",
                )
            expected_generation = (
                int(generation)
                if generation is not None
                else int(attempt.generation)
            )
            SqlErpSyncControlRepository.guard_session(
                session,
                attempt_id=attempt_id,
                generation=expected_generation,
                settings=self._control_settings,
            )
            self.assert_attempt_policy_in_session(
                session,
                attempt_id=attempt_id,
            )
            rows = tuple(
                session.scalars(
                    select(ErpSyncEvidenceManifest).where(
                        ErpSyncEvidenceManifest.attempt_id == attempt_id,
                        ErpSyncEvidenceManifest.proof_type == EVIDENCE_PROOF_SOURCE,
                        ErpSyncEvidenceManifest.source_type.in_(
                            (EVIDENCE_SOURCE_PROJECTS, EVIDENCE_SOURCE_TASKS)
                        ),
                    )
                )
            )
            if {row.source_type for row in rows} != {
                EVIDENCE_SOURCE_PROJECTS,
                EVIDENCE_SOURCE_TASKS,
            }:
                raise ApplicationConflictError(
                    "Les deux sources ERP scellées sont requises avant mutation.",
                    code="erp_sync_evidence_sources_incomplete",
                )
            if any(
                row.state not in {EVIDENCE_STATE_SEALED, EVIDENCE_STATE_READY}
                for row in rows
            ):
                raise ApplicationConflictError(
                    "Une source ERP n'est pas dans un état scellé compatible.",
                    code="erp_sync_evidence_source_not_sealed",
                )
            for row in rows:
                row.state = EVIDENCE_STATE_READY

    def assert_full_sources_ready_in_session(
        self,
        session: Session,
        *,
        attempt_id: str,
    ) -> None:
        from ..infrastructure.sql import (
            EVIDENCE_PROOF_SOURCE,
            EVIDENCE_SOURCE_PROJECTS,
            EVIDENCE_SOURCE_TASKS,
            EVIDENCE_STATE_READY,
            ErpSyncEvidenceManifest,
        )

        rows = tuple(
            session.scalars(
                select(ErpSyncEvidenceManifest).where(
                    ErpSyncEvidenceManifest.attempt_id == attempt_id,
                    ErpSyncEvidenceManifest.proof_type == EVIDENCE_PROOF_SOURCE,
                    ErpSyncEvidenceManifest.state == EVIDENCE_STATE_READY,
                )
            )
        )
        if {row.source_type for row in rows} != {
            EVIDENCE_SOURCE_PROJECTS,
            EVIDENCE_SOURCE_TASKS,
        }:
            raise ApplicationConflictError(
                "Le manifeste READY des deux sources ERP est requis avant mutation.",
                code="erp_sync_evidence_sources_not_ready",
            )

    def assert_full_sources_ready(self, *, attempt_id: str) -> None:
        with self._session_factory() as session:
            self.assert_full_sources_ready_in_session(
                session,
                attempt_id=attempt_id,
            )

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
                plaintext_amount=len(plain),
                storage_amount=len(encrypted.encode("utf-8")),
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
            plaintext_amount=len(plain),
            storage_amount=len(encrypted.encode("utf-8")),
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
                    before_bytes = self._decrypt_bytes(row.encrypted_before)
                    if hashlib.sha256(before_bytes).hexdigest() != row.before_sha256:
                        raise ApplicationConflictError(
                            "L'intégrité de la before-image ERP est invalide.",
                            code="erp_sync_checkpoint_before_integrity_invalid",
                        )
                    before = decode_canonical_json_bytes(before_bytes)
                if row.encrypted_after:
                    after_bytes = self._decrypt_bytes(row.encrypted_after)
                    if (
                        row.after_sha256 is None
                        or hashlib.sha256(after_bytes).hexdigest() != row.after_sha256
                    ):
                        raise ApplicationConflictError(
                            "L'intégrité de la after-image ERP est invalide.",
                            code="erp_sync_checkpoint_after_integrity_invalid",
                        )
                    after = decode_canonical_json_bytes(after_bytes)
                result.append((row, before, after))
            return tuple(result)

    def source_observed_at_in_session(
        self,
        session: Session,
        *,
        attempt_id: str,
        source_type: str,
    ) -> datetime | None:
        from ..infrastructure.sql import (
            EVIDENCE_PROOF_SOURCE,
            ErpSyncEvidenceManifest,
        )

        return session.scalar(
            select(ErpSyncEvidenceManifest.observed_at).where(
                ErpSyncEvidenceManifest.attempt_id == attempt_id,
                ErpSyncEvidenceManifest.source_type == source_type,
                ErpSyncEvidenceManifest.proof_type == EVIDENCE_PROOF_SOURCE,
            )
        )

    def quarantine_attempt_provenance_in_session(
        self,
        session: Session,
        *,
        attempt_id: str,
        reason_code: str,
    ) -> int:
        from ..infrastructure.sql import ErpSyncProjectQuality

        changed = 0
        rows = tuple(
            session.scalars(
                select(ErpSyncProjectQuality).where(
                    ErpSyncProjectQuality.producer_attempt_id == attempt_id
                )
            )
        )
        for row in rows:
            row.quality_state = "QUARANTINED"
            row.reason_code = str(reason_code)
            changed += 1
        return changed

    def quarantine_attempt_provenance(
        self,
        *,
        attempt_id: str,
        reason_code: str,
    ) -> int:
        from ..infrastructure.sql.session import transactional_session

        with transactional_session(self._session_factory) as session:
            return self.quarantine_attempt_provenance_in_session(
                session,
                attempt_id=attempt_id,
                reason_code=reason_code,
            )

    def mark_recovery_required_in_session(
        self,
        session: Session,
        *,
        attempt_id: str,
        reason_code: str,
    ) -> None:
        from ..infrastructure.sql import (
            RECOVERY_REQUIRED,
            ErpSyncEvidenceManifest,
            ErpSyncRecoveryState,
        )

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

    def mark_recovery_required(
        self,
        *,
        attempt_id: str,
        reason_code: str,
    ) -> None:
        from ..infrastructure.sql.session import transactional_session

        with transactional_session(self._session_factory) as session:
            self.mark_recovery_required_in_session(
                session,
                attempt_id=attempt_id,
                reason_code=reason_code,
            )

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
            lookup_fingerprint = profile_fingerprint
            if (
                self._control_settings is not None
                and profile_fingerprint == self._control_settings.config_fingerprint
            ):
                lookup_fingerprint = self.policy_fingerprint_in_session(session)
            row = session.scalar(
                select(ErpSyncBaseline).where(
                    ErpSyncBaseline.source_type == source_type,
                    ErpSyncBaseline.profile_fingerprint == lookup_fingerprint,
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
            EVIDENCE_STATE_READY,
            EVIDENCE_STATE_SEALED,
            ErpSyncBaseline,
            ErpSyncBaselineApprovalAudit,
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
            if manifest is None or manifest.state not in {
                EVIDENCE_STATE_SEALED,
                EVIDENCE_STATE_READY,
            }:
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
            session.add(
                ErpSyncBaselineApprovalAudit(
                    id=new_id(),
                    baseline_id=baseline.id,
                    manifest_id=manifest.id,
                    source_type=manifest.source_type,
                    profile_fingerprint=manifest.profile_fingerprint,
                    source_fingerprint=str(manifest.source_fingerprint),
                    row_count=int(manifest.row_count),
                    actor_name=actor,
                    reason=reason_value,
                    approved_at=_utc_now(),
                )
            )
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
            from ..infrastructure.sql import (
                EVIDENCE_PROOF_SOURCE,
                ErpSyncAttempt,
                EVIDENCE_STATE_READY,
                EVIDENCE_STATE_SEALED,
                ErpSyncEvidenceManifest,
            )
            attempt = session.get(ErpSyncAttempt, attempt_id)
            if (
                attempt is None
                or attempt.status != "PENDING"
                or attempt.phase != "PLAUSIBILITY_REVIEW_REQUIRED"
            ):
                raise ApplicationConflictError(
                    "La tentative ERP n'attend pas une approbation de plausibilité.",
                    code="erp_sync_plausibility_review_not_pending",
                )
            self.assert_attempt_policy_in_session(
                session,
                attempt_id=attempt_id,
            )
            manifest = session.scalar(
                select(ErpSyncEvidenceManifest).where(
                    ErpSyncEvidenceManifest.attempt_id == attempt_id,
                    ErpSyncEvidenceManifest.source_type == source_type,
                    ErpSyncEvidenceManifest.proof_type == EVIDENCE_PROOF_SOURCE,
                    ErpSyncEvidenceManifest.source_fingerprint == source_fingerprint,
                )
            )
            if manifest is None or manifest.state not in {
                EVIDENCE_STATE_SEALED,
                EVIDENCE_STATE_READY,
            }:
                raise ApplicationConflictError(
                    "L'exception ne correspond pas à une source scellée de cette tentative.",
                    code="erp_sync_plausibility_exception_source_mismatch",
                )
            payload = self.load_source_in_session(
                session,
                manifest_id=str(manifest.id),
            )
            project_counts = (
                self._project_counts_from_payload(payload)
                if source_type == "TASKS"
                else {}
            )
            captured = ErpSyncCapturedSource(
                source_type=source_type,
                manifest_id=str(manifest.id),
                source_fingerprint=str(manifest.source_fingerprint),
                row_count=int(manifest.row_count),
                project_counts=project_counts,
                payload=payload,
            )
            active_task_targets = (
                len(project_counts)
                if source_type == "TASKS"
                else 0
            )
            current_result = self.validate_plausibility(
                captured,
                active_task_targets=active_task_targets,
                active_task_projects=(
                    tuple(project_counts)
                    if source_type == "TASKS"
                    else None
                ),
            )
            valid_scope = (
                scope == "GLOBAL"
                and any(
                    not item.startswith("erp_sync_project_ratio_too_low:")
                    for item in current_result.diagnostics
                )
            ) or scope in current_result.rejected_projects
            if not valid_scope:
                raise ApplicationConflictError(
                    "Le périmètre demandé ne correspond pas à un échec de plausibilité courant.",
                    code="erp_sync_plausibility_exception_scope_mismatch",
                )
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

    def advance_approved_baselines_in_session(
        self,
        session: Session,
        *,
        attempt_id: str,
    ) -> None:
        """Advance only already-human-approved comparable baselines after full success."""
        from ..infrastructure.sql import (
            EVIDENCE_PROOF_SOURCE,
            EVIDENCE_SOURCE_PROJECTS,
            EVIDENCE_SOURCE_TASKS,
            EVIDENCE_STATE_READY,
            ErpSyncBaseline,
            ErpSyncEvidenceManifest,
        )

        manifests = tuple(
            session.scalars(
                select(ErpSyncEvidenceManifest).where(
                    ErpSyncEvidenceManifest.attempt_id == attempt_id,
                    ErpSyncEvidenceManifest.proof_type == EVIDENCE_PROOF_SOURCE,
                    ErpSyncEvidenceManifest.state == EVIDENCE_STATE_READY,
                    ErpSyncEvidenceManifest.source_type.in_(
                        (EVIDENCE_SOURCE_PROJECTS, EVIDENCE_SOURCE_TASKS)
                    ),
                )
            )
        )
        if len(manifests) != 2:
            raise ApplicationConflictError(
                "Les deux preuves source scellées sont requises pour publier le baseline.",
                code="erp_sync_baseline_publication_incomplete",
            )
        for manifest in manifests:
            baseline = session.scalar(
                select(ErpSyncBaseline).where(
                    ErpSyncBaseline.source_type == manifest.source_type,
                    ErpSyncBaseline.profile_fingerprint == manifest.profile_fingerprint,
                )
            )
            if baseline is None:
                raise ApplicationConflictError(
                    "Un baseline humain approuvé est requis avant publication automatique.",
                    code="erp_sync_baseline_required",
                )
            baseline.source_fingerprint = str(manifest.source_fingerprint)
            baseline.row_count = int(manifest.row_count)
            if manifest.source_type == EVIDENCE_SOURCE_TASKS:
                counts = self._project_counts_from_payload(
                    self.load_source_in_session(
                        session,
                        manifest_id=str(manifest.id),
                    )
                )
                baseline.encrypted_project_counts = (
                    self._encrypt_bytes(canonical_json_bytes(counts))
                    if counts
                    else None
                )

    def advance_approved_baselines(self, *, attempt_id: str) -> None:
        from ..infrastructure.sql.session import transactional_session

        with transactional_session(self._session_factory) as session:
            self.advance_approved_baselines_in_session(
                session,
                attempt_id=attempt_id,
            )

    def _baseline_project_counts(self, baseline) -> dict[str, int]:
        if not baseline.encrypted_project_counts:
            return {}
        try:
            value = decode_canonical_json_bytes(
                self._decrypt_bytes(baseline.encrypted_project_counts)
            )
        except ApplicationConflictError as exc:
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
        active_task_projects: Sequence[str] | None = None,
    ) -> ErpSyncPlausibilityResult:
        from ..infrastructure.sql import (
            ErpSyncBaseline,
            ErpSyncEvidenceManifest,
            ErpSyncPlausibilityException,
        )
        from ..infrastructure.sql.session import transactional_session

        with transactional_session(self._session_factory) as session:
            manifest = session.get(ErpSyncEvidenceManifest, captured.manifest_id)
            if manifest is None:
                raise ApplicationValidationError(
                    "La preuve ERP est introuvable.",
                    code="erp_sync_evidence_not_found",
                )
            attempt_id = str(manifest.attempt_id)
            baseline = session.scalar(
                select(ErpSyncBaseline).where(
                    ErpSyncBaseline.source_type == captured.source_type,
                    ErpSyncBaseline.profile_fingerprint
                    == manifest.profile_fingerprint,
                )
            )
            if baseline is None:
                return ErpSyncPlausibilityResult(
                    accepted=False,
                    diagnostics=("erp_sync_baseline_required",),
                    reviewable=False,
                )

            # An explicitly empty task target set is a legitimate empty phase,
            # not an amputated source compared with a historical positive baseline.
            current_task_projects = (
                {
                    str(project).strip()
                    for project in active_task_projects
                    if str(project).strip()
                }
                if active_task_projects is not None
                else None
            )
            effective_task_targets = (
                len(current_task_projects)
                if current_task_projects is not None
                else active_task_targets
            )
            intentionally_empty_tasks = (
                captured.source_type == "TASKS"
                and effective_task_targets == 0
            )
            global_diagnostics: list[str] = []
            project_diagnostics: list[tuple[str, str]] = []
            floor = (
                self.settings.min_projects
                if captured.source_type == "PROJECTS"
                else self.settings.min_task_rows if effective_task_targets else 0
            )
            if captured.row_count < floor:
                global_diagnostics.append("erp_sync_source_below_floor")
            baseline_count = int(baseline.row_count)
            if (
                not intentionally_empty_tasks
                and baseline_count > 0
                and captured.row_count / baseline_count < self.settings.min_source_ratio
            ):
                global_diagnostics.append("erp_sync_source_ratio_too_low")

            if not intentionally_empty_tasks:
                baseline_projects = self._baseline_project_counts(baseline)
                for project, previous_value in baseline_projects.items():
                    if (
                        current_task_projects is not None
                        and project not in current_task_projects
                    ):
                        continue
                    previous = int(previous_value)
                    if previous <= 0:
                        continue
                    current = int(captured.project_counts.get(project, 0))
                    if current / previous < self.settings.min_source_ratio:
                        project_diagnostics.append(
                            (project, f"erp_sync_project_ratio_too_low:{project}")
                        )

            now = _utc_now()
            current = tuple(
                session.scalars(
                    select(ErpSyncPlausibilityException).where(
                        ErpSyncPlausibilityException.attempt_id == attempt_id,
                        ErpSyncPlausibilityException.source_type
                        == captured.source_type,
                        ErpSyncPlausibilityException.source_fingerprint
                        == captured.source_fingerprint,
                        ErpSyncPlausibilityException.expires_at > now,
                    )
                )
            )
            approved = {str(row.threshold_scope) for row in current}
            remaining_global = tuple(
                item for item in global_diagnostics if "GLOBAL" not in approved
            )
            rejected_projects = tuple(
                project
                for project, _diagnostic in project_diagnostics
                if project not in approved
            )
            remaining_local = tuple(
                diagnostic
                for project, diagnostic in project_diagnostics
                if project not in approved
            )
            return ErpSyncPlausibilityResult(
                accepted=not remaining_global,
                diagnostics=remaining_global + remaining_local,
                rejected_projects=rejected_projects,
                reviewable=bool(remaining_global or remaining_local),
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

    def committed_mutation_count_in_session(
        self,
        session: Session,
        *,
        attempt_id: str,
    ) -> int:
        from ..infrastructure.sql import ErpSyncMutationEvidence

        return int(
            session.scalar(
                select(func.count(ErpSyncMutationEvidence.id)).where(
                    ErpSyncMutationEvidence.attempt_id == attempt_id,
                    ErpSyncMutationEvidence.committed_at.is_not(None),
                    ErpSyncMutationEvidence.after_sha256.is_not(None),
                )
            )
            or 0
        )

    def committed_mutation_count(self, *, attempt_id: str) -> int:
        with self._session_factory() as session:
            return self.committed_mutation_count_in_session(
                session,
                attempt_id=attempt_id,
            )

    def finalize_attempt(
        self,
        *,
        attempt_id: str,
        successful: bool,
    ) -> None:
        # Capacity ownership ends with the terminal attempt, independently of
        # retention/checkpoint bookkeeping below. The release is idempotent and
        # retained evidence remains charged through used_bytes.
        self.release_reservation(attempt_id=attempt_id)
        from ..infrastructure.sql import (
            RECOVERY_AVAILABLE,
            RECOVERY_NONE,
            RECOVERY_REQUIRED,
            ErpSyncEvidenceManifest,
            ErpSyncRecoveryState,
        )
        from ..infrastructure.sql.session import transactional_session

        now = _utc_now()
        # Raw/encrypted payload retention is intentionally shorter than
        # durable run/result history. Successful publication keeps metadata
        # authority, but payload bytes remain only for the bounded evidence
        # retention window.
        retention_days = self.settings.failure_retention_days
        with transactional_session(self._session_factory) as session:
            recovery = session.scalar(
                select(ErpSyncRecoveryState).where(
                    ErpSyncRecoveryState.attempt_id == attempt_id
                )
            )
            from ..infrastructure.sql import (
                EVIDENCE_PROOF_CHECKPOINT,
                EVIDENCE_SOURCE_CHECKPOINT,
                ErpSyncMutationEvidence,
            )
            mutation_rows = tuple(
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
            checkpoint = session.scalar(
                select(ErpSyncEvidenceManifest).where(
                    ErpSyncEvidenceManifest.attempt_id == attempt_id,
                    ErpSyncEvidenceManifest.source_type == EVIDENCE_SOURCE_CHECKPOINT,
                    ErpSyncEvidenceManifest.proof_type == EVIDENCE_PROOF_CHECKPOINT,
                )
            )
            if checkpoint is not None:
                checkpoint.row_count = len(mutation_rows)
                checkpoint.ciphertext_bytes = sum(
                    len(str(row.encrypted_before or "").encode("utf-8"))
                    + len(str(row.encrypted_after or "").encode("utf-8"))
                    for row in mutation_rows
                )
                checkpoint.plaintext_bytes = sum(
                    len(self._decrypt_bytes(row.encrypted_before))
                    if row.encrypted_before
                    else 0
                    for row in mutation_rows
                ) + sum(
                    len(self._decrypt_bytes(row.encrypted_after))
                    if row.encrypted_after
                    else 0
                    for row in mutation_rows
                )
                checkpoint.content_sha256 = content_sha256(
                    [
                        {
                            "id": row.id,
                            "unit_type": row.unit_type,
                            "entity_type": row.entity_type,
                            "entity_key": row.entity_key,
                            "before_sha256": row.before_sha256,
                            "after_sha256": row.after_sha256,
                            "committed_at": row.committed_at,
                        }
                        for row in mutation_rows
                    ]
                )

            if recovery is None:
                recovery = ErpSyncRecoveryState(
                    attempt_id=attempt_id,
                    state=RECOVERY_AVAILABLE if mutation_rows else RECOVERY_NONE,
                    pinned=False,
                )
                session.add(recovery)
            elif (
                recovery.state == RECOVERY_NONE
                and mutation_rows
            ):
                recovery.state = RECOVERY_AVAILABLE
            pin = recovery.state == RECOVERY_REQUIRED

            for manifest in session.scalars(
                select(ErpSyncEvidenceManifest).where(
                    ErpSyncEvidenceManifest.attempt_id == attempt_id
                )
            ):
                manifest.pinned = bool(manifest.pinned or pin)
                if not manifest.pinned:
                    manifest.expires_at = now + timedelta(days=retention_days)

    @staticmethod
    def _required_automatic_protocol_tables() -> tuple[str, ...]:
        """Return the current 643B persistence surface required for automatic use."""
        from ..infrastructure.sql import (
            AcumaticaProjectTaskSyncProjectResult,
            AcumaticaProjectTaskSyncRun,
            ErpSyncAttempt,
            ErpSyncBaseline,
            ErpSyncBaselineApprovalAudit,
            ErpSyncCompensationReceipt,
            ErpSyncControl,
            ErpSyncEvidenceChunk,
            ErpSyncEvidenceManifest,
            ErpSyncEvidenceReservation,
            ErpSyncMutationEvidence,
            ErpSyncOccurrence,
            ErpSyncPlausibilityException,
            ErpSyncProjectQuality,
            ErpSyncRecoveryState,
        )

        models = (
            ErpSyncControl,
            ErpSyncOccurrence,
            ErpSyncAttempt,
            AcumaticaProjectTaskSyncRun,
            AcumaticaProjectTaskSyncProjectResult,
            ErpSyncEvidenceManifest,
            ErpSyncEvidenceChunk,
            ErpSyncEvidenceReservation,
            ErpSyncBaseline,
            ErpSyncBaselineApprovalAudit,
            ErpSyncPlausibilityException,
            ErpSyncMutationEvidence,
            ErpSyncProjectQuality,
            ErpSyncRecoveryState,
            ErpSyncCompensationReceipt,
        )
        return tuple(model.__table__.name for model in models)

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
                required_tables = self._required_automatic_protocol_tables()
                if not all(inspector.has_table(name) for name in required_tables):
                    return False
                from ..infrastructure.sql import (
                    ERP_SYNC_CONTROL_ROW_ID,
                    RECOVERY_REQUIRED,
                    ErpSyncControl,
                    ErpSyncRecoveryState,
                )
                control = session.get(ErpSyncControl, ERP_SYNC_CONTROL_ROW_ID)
                if (
                    control is None
                    or int(control.protocol_version) != ERP_SYNC_PROTOCOL_VERSION
                ):
                    return False
                recovery_required = session.scalar(
                    select(ErpSyncRecoveryState.id)
                    .where(ErpSyncRecoveryState.state == RECOVERY_REQUIRED)
                    .limit(1)
                )
                if recovery_required is not None:
                    return False
                if profile_fingerprint != self._control_settings.config_fingerprint:
                    return False
                current_policy = self.policy_fingerprint_in_session(session)
                count = int(
                    session.scalar(
                        select(func.count(ErpSyncBaseline.id)).where(
                            ErpSyncBaseline.profile_fingerprint == current_policy,
                            ErpSyncBaseline.source_type.in_(("PROJECTS", "TASKS")),
                        )
                    )
                    or 0
                )
                return count == 2
        except Exception:
            return False

    @staticmethod
    def _preview_hash(
        attempt_id: str,
        rows: Sequence[
            tuple[Any, Mapping[str, Any] | None, Mapping[str, Any] | None]
        ],
        *,
        control_generation: int,
    ) -> str:
        material = {
            "attempt_id": attempt_id,
            "control_generation": int(control_generation),
            "evidence": [
                {
                    "evidence_id": row.id,
                    "entity_type": row.entity_type,
                    "entity_key": row.entity_key,
                    "before_sha256": row.before_sha256,
                    "after_sha256": row.after_sha256,
                }
                for row, _before, _after in sorted(
                    rows,
                    key=lambda item: (
                        str(item[0].unit_type),
                        str(item[0].entity_type),
                        str(item[0].entity_key),
                        str(item[0].id),
                    ),
                )
                if row.committed_at is not None and row.after_sha256 is not None
            ],
        }
        return content_sha256(material)

    @staticmethod
    def _compensation_scalar(
        session: Session,
        model: Any,
        *criteria: Any,
        lock_for_compensation: bool,
    ) -> Any | None:
        statement = select(model).where(*criteria)
        if lock_for_compensation and session.get_bind().dialect.name == "mssql":
            statement = statement.with_hint(
                model,
                "WITH (UPDLOCK, HOLDLOCK)",
                dialect_name="mssql",
            )
        return session.scalar(statement)

    def _current_owned_state(
        self,
        session: Session,
        row: Any,
        *,
        lock_for_compensation: bool = False,
    ) -> Mapping[str, Any] | None:
        from ..infrastructure.sql import Project, TaskCatalogEntry, TaskCatalogProjectSyncState

        if row.entity_type == "PROJECT":
            project = self._compensation_scalar(
                session,
                Project,
                (Project.erp_external_id == row.entity_key)
                | (Project.number == row.entity_key),
                lock_for_compensation=lock_for_compensation,
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
                task = self._compensation_scalar(
                    session,
                    TaskCatalogEntry,
                    TaskCatalogEntry.project_number == project_number,
                    TaskCatalogEntry.task_code == task_code,
                    lock_for_compensation=lock_for_compensation,
                )
            else:
                task = self._compensation_scalar(
                    session,
                    TaskCatalogEntry,
                    TaskCatalogEntry.erp_task_id == row.entity_key,
                    lock_for_compensation=lock_for_compensation,
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
                "operational_responsible_contact_id": task.operational_responsible_contact_id,
                "coordinator_contact_id": task.coordinator_contact_id,
                "preferred_resource_id": task.preferred_resource_id,
                "preferred_resource_version": int(task.preferred_resource_version or 1),
            }
        if row.entity_type == "TASK_SYNC_STATE":
            state = self._compensation_scalar(
                session,
                TaskCatalogProjectSyncState,
                TaskCatalogProjectSyncState.project_number == row.entity_key,
                lock_for_compensation=lock_for_compensation,
            )
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

    def _assert_attempt_evidence_compatible_in_session(
        self,
        session: Session,
        *,
        attempt_id: str,
    ) -> None:
        from ..infrastructure.sql import (
            EVIDENCE_STATE_EXPIRED,
            EVIDENCE_STATE_PURGED,
            ERP_EVIDENCE_PROTOCOL_VERSION,
            ErpSyncEvidenceManifest,
        )

        manifests = tuple(
            session.scalars(
                select(ErpSyncEvidenceManifest).where(
                    ErpSyncEvidenceManifest.attempt_id == attempt_id
                )
            )
        )
        if not manifests:
            raise ApplicationConflictError(
                "Aucune preuve ERP durable n'est disponible pour cette tentative.",
                code="erp_sync_compensation_evidence_missing",
            )
        if any(
            int(row.protocol_version) != ERP_EVIDENCE_PROTOCOL_VERSION
            for row in manifests
        ):
            raise ApplicationConflictError(
                "La version de protocole de preuve ERP est incompatible.",
                code="erp_sync_evidence_protocol_incompatible",
            )
        from ..infrastructure.sql import ErpSyncAttempt

        attempt = session.get(ErpSyncAttempt, attempt_id)
        if (
            self._control_settings is None
            or attempt is None
            or not attempt.policy_fingerprint
        ):
            raise ApplicationConflictError(
                "Le profil de preuve ERP n'est plus compatible avec le contrôle courant.",
                code="erp_sync_evidence_configuration_incompatible",
            )
        current_policy = self.policy_fingerprint_in_session(session)
        if (
            str(attempt.policy_fingerprint) != current_policy
            or any(
                str(row.profile_fingerprint) != current_policy
                for row in manifests
            )
        ):
            raise ApplicationConflictError(
                "Le profil de preuve ERP n'est plus compatible avec la politique courante.",
                code="erp_sync_evidence_configuration_incompatible",
            )
        bind = session.get_bind()
        if bind.dialect.name == "mssql":
            sql_now = session.scalar(select(func.sysutcdatetime()))
            now = _normalized_utc(sql_now)
        else:
            now = _utc_now()
        if any(
            row.state in {EVIDENCE_STATE_EXPIRED, EVIDENCE_STATE_PURGED}
            or (
                row.expires_at is not None
                and _normalized_utc(row.expires_at) <= now
            )
            for row in manifests
        ):
            raise ApplicationConflictError(
                "La preuve ERP requise pour la compensation est expirée.",
                code="erp_sync_compensation_evidence_expired",
            )

    def _new_row_has_dependencies(
        self,
        session: Session,
        row: Any,
        *,
        lock_for_compensation: bool = False,
    ) -> bool:
        from ..infrastructure.sql import (
            Project,
            TaskCatalogEntry,
            WorkforceRequest,
            WorkPackage,
        )

        if row.entity_type == "PROJECT":
            project = self._compensation_scalar(
                session,
                Project,
                (Project.erp_external_id == row.entity_key)
                | (Project.number == row.entity_key),
                lock_for_compensation=lock_for_compensation,
            )
            if project is None:
                return True
            package_statement = select(WorkPackage).where(
                WorkPackage.project_id == project.id
            ).limit(1)
            request_statement = select(WorkforceRequest).where(
                WorkforceRequest.project_id == project.id
            ).limit(1)
            if lock_for_compensation and session.get_bind().dialect.name == "mssql":
                package_statement = package_statement.with_hint(
                    WorkPackage,
                    "WITH (UPDLOCK, HOLDLOCK)",
                    dialect_name="mssql",
                )
                request_statement = request_statement.with_hint(
                    WorkforceRequest,
                    "WITH (UPDLOCK, HOLDLOCK)",
                    dialect_name="mssql",
                )
            has_package = session.scalar(package_statement)
            has_request = session.scalar(request_statement)
            return has_package is not None or has_request is not None
        if row.entity_type == "TASK":
            if ":" in row.entity_key:
                project_number, task_code = row.entity_key.split(":", 1)
                task = self._compensation_scalar(
                    session,
                    TaskCatalogEntry,
                    TaskCatalogEntry.project_number == project_number,
                    TaskCatalogEntry.task_code == task_code,
                    lock_for_compensation=lock_for_compensation,
                )
            else:
                task = self._compensation_scalar(
                    session,
                    TaskCatalogEntry,
                    TaskCatalogEntry.erp_task_id == row.entity_key,
                    lock_for_compensation=lock_for_compensation,
                )
            if task is None:
                return True
            dependency_statement = select(WorkPackage).where(
                WorkPackage.task_catalog_item_id == task.id
            ).limit(1)
            if lock_for_compensation and session.get_bind().dialect.name == "mssql":
                dependency_statement = dependency_statement.with_hint(
                    WorkPackage,
                    "WITH (UPDLOCK, HOLDLOCK)",
                    dialect_name="mssql",
                )
            return session.scalar(dependency_statement) is not None
        return False

    def compensation_preview(self, *, attempt_id: str) -> dict[str, Any]:
        from ..infrastructure.sql.erp_sync_control_repository import (
            SqlErpSyncControlRepository,
        )
        from ..infrastructure.sql.session import transactional_session

        if self._control_settings is None:
            raise ApplicationUnavailableError(
                "Le profil de contrôle ERP requis pour la compensation est absent.",
                code="erp_sync_recovery_control_unavailable",
            )
        with transactional_session(self._session_factory) as session:
            control = SqlErpSyncControlRepository.guard_recovery_session(
                session,
                settings=self._control_settings,
            )
            control_generation = int(control.generation)
            self._assert_attempt_evidence_compatible_in_session(
                session,
                attempt_id=attempt_id,
            )
        rows = self.mutation_payloads(attempt_id=attempt_id)
        if not rows:
            raise ApplicationValidationError(
                "Aucune preuve de mutation n'est disponible pour cette tentative.",
                code="erp_sync_compensation_evidence_missing",
            )
        conflicts: list[str] = []
        eligible = 0
        with self._session_factory() as session:
            for row, before, after in rows:
                if row.committed_at is None or row.after_sha256 is None or after is None:
                    continue
                eligible += 1
                current = self._current_owned_state(
                    session,
                    row,
                    lock_for_compensation=True,
                )
                if current is None or content_sha256(current) != row.after_sha256:
                    conflicts.append(f"{row.entity_type}:{row.entity_key}")
                    continue
                if before is None and self._new_row_has_dependencies(
                    session,
                    row,
                    lock_for_compensation=True,
                ):
                    conflicts.append(
                        f"dependency:{row.entity_type}:{row.entity_key}"
                    )
        preview_hash = self._preview_hash(
            attempt_id,
            rows,
            control_generation=control_generation,
        )
        return {
            "attempt_id": attempt_id,
            "control_generation": control_generation,
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
            ERP_OCCURRENCE_FAILED,
            RECOVERY_RESOLVED,
            ErpSyncAttempt,
            ErpSyncCompensationReceipt,
            ErpSyncMutationEvidence,
            ErpSyncOccurrence,
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
            control = SqlErpSyncControlRepository.guard_recovery_session(
                session,
                settings=self._control_settings,
            )
            control_generation = int(control.generation)
            self._assert_attempt_evidence_compatible_in_session(
                session,
                attempt_id=attempt_id,
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
                before = None
                after = None
                if row.encrypted_before:
                    before_bytes = self._decrypt_bytes(row.encrypted_before)
                    if hashlib.sha256(before_bytes).hexdigest() != row.before_sha256:
                        raise ApplicationConflictError(
                            "L'intégrité de la before-image ERP est invalide.",
                            code="erp_sync_checkpoint_before_integrity_invalid",
                        )
                    before = decode_canonical_json_bytes(before_bytes)
                if row.encrypted_after:
                    after_bytes = self._decrypt_bytes(row.encrypted_after)
                    if (
                        row.after_sha256 is None
                        or hashlib.sha256(after_bytes).hexdigest() != row.after_sha256
                    ):
                        raise ApplicationConflictError(
                            "L'intégrité de la after-image ERP est invalide.",
                            code="erp_sync_checkpoint_after_integrity_invalid",
                        )
                    after = decode_canonical_json_bytes(after_bytes)
                payload_rows.append((row, before, after))
            if self._preview_hash(
                attempt_id,
                payload_rows,
                control_generation=control_generation,
            ) != str(preview_hash):
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
                if before is None and self._new_row_has_dependencies(session, row):
                    raise ApplicationConflictError(
                        "Une nouvelle dépendance locale interdit la compensation.",
                        code="erp_sync_compensation_dependency_changed",
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
                        previous_manager_id = project.project_manager_external_id
                        previous_manager_name = project.project_manager_name
                        restored_manager_id = before.get(
                            "project_manager_external_id"
                        )
                        restored_manager_name = before.get("project_manager_name")
                        if previous_manager_id != restored_manager_id:
                            import json as _json
                            from ..application.project_managers import (
                                PROJECT_MANAGER_SOURCE_ERP,
                            )
                            from ..infrastructure.sql.project_manager_models import (
                                ProjectManagerAudit,
                            )

                            session.add(
                                ProjectManagerAudit(
                                    project_id=project.id,
                                    action="ERP_PRIMARY_COMPENSATED",
                                    source=PROJECT_MANAGER_SOURCE_ERP,
                                    actor_user_id=None,
                                    before_json=_json.dumps(
                                        {
                                            "employee_external_id": previous_manager_id,
                                            "display_name": previous_manager_name,
                                        },
                                        ensure_ascii=False,
                                        sort_keys=True,
                                        separators=(",", ":"),
                                    ),
                                    after_json=_json.dumps(
                                        {
                                            "employee_external_id": restored_manager_id,
                                            "display_name": restored_manager_name,
                                        },
                                        ensure_ascii=False,
                                        sort_keys=True,
                                        separators=(",", ":"),
                                    ),
                                    resulting_version=None,
                                )
                            )
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
                        # Preserve local foreign keys/principal authority; revoke
                        # ERP-owned state rather than destructively deleting a row.
                        task.active = False
                        task.erp_task_id = None
                        task.account_group = None
                        task.cost_code = None
                        task.inventory_id = None
                        task.budget_amount_cad = None
                        task.budget_actual_cad = None
                        task.budget_diagnostic = None
                        task.erp_budget_last_success_at = None
                    else:
                        erp_owned_fields = (
                            "label",
                            "status",
                            "active",
                            "billing_rule",
                            "allocation_rule",
                            "completion_percent",
                            "erp_created_at",
                            "branch",
                            "approver_name",
                            "cv_enabled",
                            "time_entry_enabled",
                            "expenses_enabled",
                            "erp_task_id",
                            "account_group",
                            "cost_code",
                            "inventory_id",
                            "budget_amount_cad",
                            "budget_actual_cad",
                            "budget_diagnostic",
                            "erp_budget_last_success_at",
                        )
                        for field in erp_owned_fields:
                            if field in before:
                                setattr(task, field, before.get(field))

                    # Workforce eligibility/class/cost/budget-hours are derived
                    # reference-policy state. Never restore their historical
                    # serialized values as truth; recompute through the canonical
                    # policy against current Standards/overrides/costs.
                    from ..infrastructure.sql import SqlTaskCatalogWorkforcePolicy
                    projection = SqlTaskCatalogWorkforcePolicy(
                        session
                    ).project_task_projection(
                        project_number=task.project_number,
                        task_code=task.task_code,
                        budget_amount_cad=task.budget_amount_cad,
                    )
                    task.workforce_eligible = projection.eligible
                    task.resource_class_code = projection.resource_class_code
                    task.average_hourly_cost_cad = projection.average_hourly_cost_cad
                    task.budget_hours = projection.budget_hours
                    task.workforce_diagnostics = (
                        json.dumps(
                            list(projection.diagnostics),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        if projection.diagnostics
                        else None
                    )
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
            from ..infrastructure.sql import ErpSyncEvidenceManifest
            resolution_expiry = _utc_now() + timedelta(
                days=self.settings.failure_retention_days
            )
            for manifest in session.scalars(
                select(ErpSyncEvidenceManifest).where(
                    ErpSyncEvidenceManifest.attempt_id == attempt_id
                )
            ):
                manifest.pinned = False
                manifest.expires_at = resolution_expiry
            affected_projects = {
                str(before.get("number"))
                for row, before, _after in payload_rows
                if row.entity_type == "PROJECT"
                and isinstance(before, dict)
                and before.get("number")
            }
            affected_projects.update(
                str(row.entity_key)
                for row, _before, _after in payload_rows
                if row.entity_type == "TASK_SYNC_STATE"
            )
            from ..infrastructure.sql import ErpSyncProjectQuality
            for project_number in affected_projects:
                quality = session.get(ErpSyncProjectQuality, project_number)
                if quality is not None:
                    quality.quality_state = "STALE"
                    quality.producer_attempt_id = None
                    quality.reason_code = "erp_sync_compensation_requires_refresh"
                    quality.validated_at = _utc_now()
            if str(control.last_success_attempt_id or "") == attempt_id:
                control.last_success_attempt_id = None
            compensated_attempt = session.get(ErpSyncAttempt, attempt_id)
            if compensated_attempt is not None:
                occurrence = session.get(
                    ErpSyncOccurrence,
                    compensated_attempt.occurrence_id,
                )
                if (
                    occurrence is not None
                    and str(occurrence.success_attempt_id or "") == attempt_id
                ):
                    occurrence.success_attempt_id = None
                    occurrence.status = ERP_OCCURRENCE_FAILED
                    occurrence.diagnostic = (
                        "Le succès ERP précédent a été compensé et requiert une nouvelle synchronisation."
                    )

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
                    RECOVERY_EXPIRED,
                    RECOVERY_REQUIRED,
                    RECOVERY_RESOLVED,
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
                            reservation.used_plaintext_bytes = 0
                        if (
                            recovery is not None
                            and recovery.state not in {
                                RECOVERY_REQUIRED,
                                RECOVERY_RESOLVED,
                            }
                        ):
                            recovery.state = RECOVERY_EXPIRED
                            recovery.pinned = False
        return purged
