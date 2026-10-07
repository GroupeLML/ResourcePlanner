from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from email.message import EmailMessage
from email.policy import SMTP
import hashlib
from io import BytesIO
import re
from typing import Protocol, Sequence
from zipfile import ZIP_DEFLATED, ZipFile

from ..domain.project_communication import (
    ProjectCommunicationAssignment,
    ProjectCommunicationProjection,
    build_project_communication_projection,
)
from ..domain.project_communication_messages import (
    MESSAGE_KIND_PLANNING_CHANGE,
    MESSAGE_KIND_WEEKLY_CONFIRMATION,
    ProjectCommunicationDraft,
    ProjectCommunicationMessageBatch,
    ProjectMessageDiagnostic,
    build_project_confirmation_batch,
    build_project_delta_batch,
    project_projection_fingerprint,
)
from .communications import (
    DELIVERY_PROVIDER_SMTP,
    DELIVERY_STATUS_SENT,
    STATUS_APPROVED,
    STATUS_CANCELLED,
    STATUS_COMMUNICATED,
    STATUS_PREPARED,
    CommunicationBatchRecord,
    CommunicationDeliveryRecord,
    CommunicationMessageRecord,
    CommunicationTransportMessage,
    CommunicationTransportPort,
)
from .smtp_settings import SmtpConfigurationService
from .errors import (
    ApplicationConflictError,
    ApplicationNotFoundError,
    ApplicationUnavailableError,
    ApplicationValidationError,
    call_application_port,
)


MODEL_VERSION_PROJECT_V2 = "project_v2"


@dataclass(frozen=True, slots=True)
class ProjectCommunicationReviewInput:
    message_key: str
    include: bool = True
    subject: str | None = None
    body: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectCommunicationWorkflowPreview:
    week_start: date
    mode: str
    snapshot_fingerprint: str
    drafts: tuple[ProjectCommunicationDraft, ...]
    diagnostics: tuple[ProjectMessageDiagnostic, ...]
    has_communicated_baseline: bool
    baseline_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectCommunicationDraftDownload:
    filename: str
    media_type: str
    content: bytes


_DOWNLOAD_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _download_filename_fragment(value: str) -> str:
    cleaned = _DOWNLOAD_FILENAME_UNSAFE.sub("-", str(value or "").strip()).strip("-._")
    return (cleaned[:80] or "message")


def _project_message_download_filename(message: CommunicationMessageRecord) -> str:
    identity = message.project_id or message.message_key or message.id
    prefix = "projet" if message.project_id else "message"
    return f"{prefix}-{_download_filename_fragment(identity)}.eml"


def _project_message_eml(message: CommunicationMessageRecord) -> bytes:
    recipient = str(message.recipient_email or "").strip()
    to_emails = tuple(message.to_emails) or ((recipient,) if recipient else ())
    if not to_emails:
        raise ApplicationValidationError(
            "Le message projet ne possède aucun destinataire To téléchargeable.",
            code="project_communication_download_recipient_missing",
            context={"message_id": message.id},
        )

    mime = EmailMessage(policy=SMTP)
    mime["To"] = ", ".join(to_emails)
    if message.cc_emails:
        mime["Cc"] = ", ".join(message.cc_emails)
    mime["Subject"] = message.subject
    mime.set_content(message.body)
    return mime.as_bytes(policy=SMTP)


class ProjectCommunicationRepositoryPort(Protocol):
    def list_assignments(
        self,
        *,
        week_start: date,
        week_end: date,
    ) -> Sequence[ProjectCommunicationAssignment]: ...


class ProjectCommunicationWorkflowRepositoryPort(Protocol):
    def latest_communicated_project_snapshot(
        self,
        *,
        week_start: date,
    ) -> tuple[str, ProjectCommunicationProjection] | None: ...

    def create_project_prepared_batch(
        self,
        *,
        week_start: date,
        kind: str,
        fingerprint: str,
        actor_name: str,
        messages: Sequence[tuple[ProjectCommunicationDraft, bool]],
        snapshot: ProjectCommunicationProjection,
    ) -> CommunicationBatchRecord: ...

    def has_duplicate_batch(
        self,
        *,
        week_start: date,
        kind: str,
        fingerprint: str,
    ) -> bool: ...

    def list_project_batches(
        self,
        *,
        week_start: date | None = None,
    ) -> Sequence[CommunicationBatchRecord]: ...

    def get_batch(self, batch_id: str) -> CommunicationBatchRecord | None: ...

    def set_batch_status(
        self,
        *,
        batch_id: str,
        status: str,
        actor_name: str,
    ) -> CommunicationBatchRecord: ...

    def mark_drafts_created(
        self,
        *,
        batch_id: str,
        provider: str,
        created_count: int,
        actor_name: str,
    ) -> CommunicationBatchRecord: ...

    def claim_smtp_delivery(
        self,
        *,
        message_id: str,
        actor_name: str,
    ) -> CommunicationDeliveryRecord | None: ...

    def complete_smtp_delivery(
        self,
        *,
        delivery_id: str,
        provider_message_id: str,
        actor_name: str,
    ) -> CommunicationDeliveryRecord: ...

    def fail_smtp_delivery(
        self,
        *,
        delivery_id: str,
        error_code: str,
        error_detail: str,
        actor_name: str,
    ) -> CommunicationDeliveryRecord: ...


class ProjectCommunicationService:
    """Project-centric communication projection and controlled draft workflow."""

    def __init__(
        self,
        repository: ProjectCommunicationRepositoryPort,
        *,
        workflow_repository: ProjectCommunicationWorkflowRepositoryPort | None = None,
        transport: CommunicationTransportPort | None = None,
        smtp_service: SmtpConfigurationService | None = None,
    ) -> None:
        self._repository = repository
        self._workflow_repository = workflow_repository
        self._transport = transport
        self._smtp_service = smtp_service

    @staticmethod
    def _normalize_week_start(value: date) -> date:
        return value - timedelta(days=value.weekday())

    def _workflow(self) -> ProjectCommunicationWorkflowRepositoryPort:
        if self._workflow_repository is None:
            raise ApplicationUnavailableError(
                "Le workflow de communication projet n'est pas configuré.",
                code="project_communication_workflow_unavailable",
            )
        return self._workflow_repository

    def project_projection(self, *, week_start: date) -> ProjectCommunicationProjection:
        week = self._normalize_week_start(week_start)
        week_end = week + timedelta(days=6)
        assignments = call_application_port(
            lambda: self._repository.list_assignments(
                week_start=week,
                week_end=week_end,
            ),
            code_prefix="project_communication_read",
            context={"week_start": week.isoformat()},
        )
        return build_project_communication_projection(
            week_start=week,
            week_end=week_end,
            assignments=tuple(assignments),
        )

    def project_preview(
        self,
        *,
        week_start: date,
    ) -> ProjectCommunicationWorkflowPreview:
        projection = self.project_projection(week_start=week_start)
        current_fingerprint = project_projection_fingerprint(projection)
        repository = self._workflow_repository
        baseline = (
            repository.latest_communicated_project_snapshot(
                week_start=projection.week_start
            )
            if repository is not None
            else None
        )
        if baseline is None:
            batch = build_project_confirmation_batch(projection)
            mode = MESSAGE_KIND_WEEKLY_CONFIRMATION
            baseline_fingerprint = None
        else:
            baseline_fingerprint, previous = baseline
            if baseline_fingerprint == current_fingerprint:
                batch = ProjectCommunicationMessageBatch(
                    drafts=(),
                    snapshot_fingerprint=current_fingerprint,
                    diagnostics=(),
                )
            else:
                batch = build_project_delta_batch(previous, projection)
            mode = MESSAGE_KIND_PLANNING_CHANGE
        return ProjectCommunicationWorkflowPreview(
            week_start=projection.week_start,
            mode=mode,
            snapshot_fingerprint=current_fingerprint,
            drafts=batch.drafts,
            diagnostics=batch.diagnostics,
            has_communicated_baseline=baseline is not None,
            baseline_fingerprint=(
                baseline[0] if baseline is not None else None
            ),
        )

    @staticmethod
    def _reviewed_draft(
        draft: ProjectCommunicationDraft,
        review: ProjectCommunicationReviewInput | None,
    ) -> tuple[ProjectCommunicationDraft, bool]:
        if review is None:
            return draft, True
        subject = draft.subject if review.subject is None else review.subject.strip()
        body = draft.body if review.body is None else review.body.strip()
        content_fingerprint = hashlib.sha256(
            (
                draft.content_fingerprint
                + "\n"
                + subject
                + "\n"
                + body
            ).encode("utf-8")
        ).hexdigest()
        return (
            replace(
                draft,
                subject=subject,
                body=body,
                content_fingerprint=content_fingerprint,
            ),
            bool(review.include),
        )

    def prepare_project_batch(
        self,
        *,
        week_start: date,
        expected_fingerprint: str,
        reviews: Sequence[ProjectCommunicationReviewInput],
        actor_name: str,
    ) -> CommunicationBatchRecord:
        repository = self._workflow()
        preview = self.project_preview(week_start=week_start)
        if str(expected_fingerprint or "").strip() != preview.snapshot_fingerprint:
            raise ApplicationConflictError(
                "Le planning projet a changé depuis la prévisualisation.",
                code="project_communication_preview_stale",
            )
        if not preview.drafts:
            raise ApplicationValidationError(
                "Aucun message projet n'est requis pour cette version du planning.",
                code="project_communication_no_messages",
            )

        review_map = {
            str(row.message_key or "").strip(): row
            for row in reviews
            if str(row.message_key or "").strip()
        }
        valid_keys = {draft.message_key for draft in preview.drafts}
        unknown = sorted(set(review_map) - valid_keys)
        if unknown:
            raise ApplicationValidationError(
                "Une révision cible un message projet inconnu.",
                code="project_communication_review_unknown",
                context={"message_keys": unknown},
            )

        stored: list[tuple[ProjectCommunicationDraft, bool]] = []
        blocked: list[str] = []
        included_count = 0
        for draft in preview.drafts:
            reviewed, included = self._reviewed_draft(
                draft,
                review_map.get(draft.message_key),
            )
            if included:
                included_count += 1
                if not reviewed.approvable:
                    blocked.append(reviewed.message_key)
                if not reviewed.subject.strip() or not reviewed.body.strip():
                    raise ApplicationValidationError(
                        "Le sujet et le corps sont requis pour un message inclus.",
                        code="project_communication_message_blank",
                        context={"message_key": reviewed.message_key},
                    )
            stored.append((reviewed, included))

        if blocked:
            raise ApplicationValidationError(
                "Un ou plusieurs messages projet ne peuvent pas être approuvés.",
                code="project_communication_not_approvable",
                context={"message_keys": blocked},
            )
        if included_count == 0:
            raise ApplicationValidationError(
                "Au moins un message projet doit rester inclus.",
                code="project_communication_no_included_messages",
            )
        if repository.has_duplicate_batch(
            week_start=preview.week_start,
            kind=preview.mode,
            fingerprint=preview.snapshot_fingerprint,
        ):
            raise ApplicationConflictError(
                "Un lot projet existe déjà pour cette version du planning.",
                code="project_communication_batch_duplicate",
            )

        snapshot = self.project_projection(week_start=preview.week_start)
        if project_projection_fingerprint(snapshot) != preview.snapshot_fingerprint:
            raise ApplicationConflictError(
                "Le planning projet a changé pendant la préparation.",
                code="project_communication_preview_stale",
            )
        return repository.create_project_prepared_batch(
            week_start=preview.week_start,
            kind=preview.mode,
            fingerprint=preview.snapshot_fingerprint,
            actor_name=str(actor_name or "").strip() or "api",
            messages=tuple(stored),
            snapshot=snapshot,
        )

    def _project_batch(self, batch_id: str) -> CommunicationBatchRecord:
        row = self._workflow().get_batch(str(batch_id or "").strip())
        if row is None or row.model_version != MODEL_VERSION_PROJECT_V2:
            raise ApplicationNotFoundError(
                "Lot de communication projet introuvable.",
                code="project_communication_batch_not_found",
                context={"batch_id": batch_id},
            )
        return row

    def _assert_current_snapshot(
        self,
        row: CommunicationBatchRecord,
        *,
        message: str,
    ) -> None:
        current = project_projection_fingerprint(
            self.project_projection(week_start=row.week_start)
        )
        if current != row.snapshot_fingerprint:
            raise ApplicationConflictError(
                message,
                code="project_communication_batch_stale",
            )

    def list_project_batches(
        self,
        *,
        week_start: date | None = None,
    ) -> tuple[CommunicationBatchRecord, ...]:
        repository = self._workflow()
        week = self._normalize_week_start(week_start) if week_start else None
        rows = tuple(repository.list_project_batches(week_start=week))
        if week is None:
            return rows
        current = project_projection_fingerprint(
            self.project_projection(week_start=week)
        )
        return tuple(
            replace(
                row,
                stale=(
                    row.status in {STATUS_PREPARED, STATUS_APPROVED}
                    and row.snapshot_fingerprint != current
                ),
            )
            for row in rows
        )

    def approve_project_batch(
        self,
        *,
        batch_id: str,
        actor_name: str,
    ) -> CommunicationBatchRecord:
        row = self._project_batch(batch_id)
        if row.status != STATUS_PREPARED:
            raise ApplicationConflictError(
                "Seul un lot projet préparé peut être approuvé.",
                code="project_communication_batch_not_prepared",
            )
        invalid = [
            message.message_key or message.id
            for message in row.messages
            if message.included
            and (
                not message.approvable
                or not message.to_emails
                or any(
                    not str(email or "").strip()
                    for email in message.to_emails
                )
            )
        ]
        if invalid:
            raise ApplicationValidationError(
                "Un message projet inclus n'est pas approuvable.",
                code="project_communication_not_approvable",
                context={"message_keys": invalid},
            )
        self._assert_current_snapshot(
            row,
            message="Le planning projet a changé depuis la préparation du lot.",
        )
        return self._workflow().set_batch_status(
            batch_id=row.id,
            status=STATUS_APPROVED,
            actor_name=actor_name,
        )

    def create_project_drafts(
        self,
        *,
        batch_id: str,
        actor_name: str,
    ) -> CommunicationBatchRecord:
        row = self._project_batch(batch_id)
        if row.status != STATUS_APPROVED:
            raise ApplicationConflictError(
                "Le lot projet doit être approuvé avant de créer les brouillons M365.",
                code="project_communication_batch_not_approved",
            )
        if row.drafts_created_at is not None:
            raise ApplicationConflictError(
                "Les brouillons M365 ont déjà été créés pour ce lot projet.",
                code="project_communication_drafts_already_created",
            )
        self._assert_current_snapshot(
            row,
            message=(
                "Le planning projet a changé depuis l'approbation; "
                "préparez un nouveau lot."
            ),
        )
        if self._transport is None:
            raise ApplicationUnavailableError(
                "Le transport Microsoft 365 n'est pas configuré sur ce serveur.",
                code="communication_transport_unavailable",
            )

        messages = tuple(
            CommunicationTransportMessage(
                audience=message.audience,
                recipient_id=message.message_key or message.recipient_id,
                recipient_email=str(message.recipient_email or "").strip(),
                subject=message.subject,
                body=message.body,
                cc_emails=message.cc_emails,
                to_emails=message.to_emails,
            )
            for message in row.messages
            if message.included
        )
        if not messages:
            raise ApplicationValidationError(
                "Aucun message projet inclus n'est disponible.",
                code="project_communication_no_included_messages",
            )
        result = self._transport.create_drafts(messages)
        if result.created_count != len(messages):
            raise ApplicationUnavailableError(
                "Le transport M365 n'a pas confirmé tous les brouillons projet.",
                code="communication_transport_incomplete",
                context={
                    "expected_count": len(messages),
                    "created_count": result.created_count,
                    "provider": result.provider,
                },
            )
        return self._workflow().mark_drafts_created(
            batch_id=row.id,
            provider=result.provider,
            created_count=result.created_count,
            actor_name=actor_name,
        )

    def project_draft_download(
        self,
        *,
        batch_id: str,
    ) -> ProjectCommunicationDraftDownload:
        row = self._project_batch(batch_id)
        if row.drafts_created_at is None:
            raise ApplicationConflictError(
                "Les brouillons M365 doivent être créés avant de télécharger leur copie.",
                code="project_communication_drafts_not_created",
            )

        messages = tuple(message for message in row.messages if message.included)
        if not messages:
            raise ApplicationValidationError(
                "Aucun message projet inclus n'est disponible au téléchargement.",
                code="project_communication_no_included_messages",
            )

        try:
            files = tuple(
                (
                    _project_message_download_filename(message),
                    _project_message_eml(message),
                )
                for message in messages
            )
        except ApplicationValidationError:
            raise
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ApplicationUnavailableError(
                "Les brouillons M365 existent, mais leur copie téléchargeable n'a pas pu être générée.",
                code="project_communication_draft_download_failed",
                context={"batch_id": row.id},
            ) from exc

        if len(files) == 1:
            filename, content = files[0]
            return ProjectCommunicationDraftDownload(
                filename=filename,
                media_type="message/rfc822",
                content=content,
            )

        archive = BytesIO()
        with ZipFile(archive, mode="w", compression=ZIP_DEFLATED) as bundle:
            for filename, content in files:
                bundle.writestr(filename, content)
        return ProjectCommunicationDraftDownload(
            filename=(
                "brouillons-m365-"
                f"{row.week_start.isoformat()}-{_download_filename_fragment(row.id)[:12]}.zip"
            ),
            media_type="application/zip",
            content=archive.getvalue(),
        )

    def send_project_smtp(
        self,
        *,
        batch_id: str,
        actor_name: str,
    ) -> CommunicationBatchRecord:
        row = self._project_batch(batch_id)
        if row.status != STATUS_APPROVED:
            raise ApplicationConflictError(
                "Le lot projet doit être approuvé avant l'envoi SMTP.",
                code="project_communication_batch_not_approved",
            )
        self._assert_current_snapshot(
            row,
            message=(
                "Le planning projet a changé depuis l'approbation; "
                "préparez un nouveau lot."
            ),
        )
        if self._smtp_service is None:
            raise ApplicationUnavailableError(
                "Le service SMTP n'est pas configuré sur ce serveur.",
                code="smtp_service_unavailable",
            )

        # Validate configuration/decryption before claiming any delivery.
        self._smtp_service.runtime_configuration(require_enabled=True)
        repository = self._workflow()
        for message in row.messages:
            if not message.included:
                continue
            existing = next(
                (
                    delivery
                    for delivery in message.deliveries
                    if delivery.provider == DELIVERY_PROVIDER_SMTP
                ),
                None,
            )
            if existing is not None and existing.status == DELIVERY_STATUS_SENT:
                continue

            claimed = repository.claim_smtp_delivery(
                message_id=message.id,
                actor_name=actor_name,
            )
            if claimed is None:
                continue

            transport_message = CommunicationTransportMessage(
                audience=message.audience,
                recipient_id=message.message_key or message.recipient_id,
                recipient_email=str(message.recipient_email or "").strip(),
                subject=message.subject,
                body=message.body,
                cc_emails=message.cc_emails,
                to_emails=message.to_emails,
            )
            message_id = (
                "<resourceplanner-"
                + claimed.id
                + chr(64)
                + "local.invalid>"
            )
            try:
                provider_message_id = self._smtp_service.send_message(
                    transport_message,
                    message_id=message_id,
                )
            except ApplicationUnavailableError as exc:
                repository.fail_smtp_delivery(
                    delivery_id=claimed.id,
                    error_code=exc.code,
                    error_detail=exc.message,
                    actor_name=actor_name,
                )
                continue

            repository.complete_smtp_delivery(
                delivery_id=claimed.id,
                provider_message_id=provider_message_id,
                actor_name=actor_name,
            )

        refreshed = self._project_batch(row.id)
        included = [message for message in refreshed.messages if message.included]
        all_sent = bool(included) and all(
            any(
                delivery.provider == DELIVERY_PROVIDER_SMTP
                and delivery.status == DELIVERY_STATUS_SENT
                for delivery in message.deliveries
            )
            for message in included
        )
        if all_sent:
            return repository.set_batch_status(
                batch_id=refreshed.id,
                status=STATUS_COMMUNICATED,
                actor_name=actor_name,
            )
        return refreshed

    def cancel_project_batch(
        self,
        *,
        batch_id: str,
        actor_name: str,
    ) -> CommunicationBatchRecord:
        row = self._project_batch(batch_id)
        if row.status not in {STATUS_PREPARED, STATUS_APPROVED}:
            raise ApplicationConflictError(
                "Ce lot projet ne peut plus être annulé.",
                code="project_communication_batch_not_cancellable",
            )
        return self._workflow().set_batch_status(
            batch_id=row.id,
            status=STATUS_CANCELLED,
            actor_name=actor_name,
        )

    def mark_project_communicated(
        self,
        *,
        batch_id: str,
        actor_name: str,
    ) -> CommunicationBatchRecord:
        row = self._project_batch(batch_id)
        if row.status != STATUS_APPROVED:
            raise ApplicationConflictError(
                "Le lot projet doit être approuvé avant d'être confirmé communiqué.",
                code="project_communication_batch_not_approved",
            )
        self._assert_current_snapshot(
            row,
            message=(
                "Le planning projet a changé depuis l'approbation; "
                "préparez un nouveau lot."
            ),
        )
        return self._workflow().set_batch_status(
            batch_id=row.id,
            status=STATUS_COMMUNICATED,
            actor_name=actor_name,
        )
