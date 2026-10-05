from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol, Sequence

from ..domain.communication_planning import (
    CommunicationBatch,
    CommunicationDraft,
    Contact,
    WeeklyAssignment,
    build_change_notification_batch,
    build_weekly_plan_batch,
    snapshot_fingerprint,
)
from ..domain.communication_review import DraftReview, apply_manual_review
from .errors import (
    ApplicationConflictError,
    ApplicationNotFoundError,
    ApplicationUnavailableError,
    ApplicationValidationError,
)


STATUS_PREPARED = "PREPARED"
STATUS_APPROVED = "APPROVED"
STATUS_CANCELLED = "CANCELLED"
STATUS_COMMUNICATED = "COMMUNICATED"

KIND_WEEKLY = "weekly_plan"
KIND_CHANGE = "planning_change"

DELIVERY_PROVIDER_SMTP = "SMTP"
DELIVERY_STATUS_PENDING = "PENDING"
DELIVERY_STATUS_SENDING = "SENDING"
DELIVERY_STATUS_SENT = "SENT"
DELIVERY_STATUS_FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class CommunicationContactRecord:
    recipient_id: str
    audience: str
    display_name: str
    email: str | None
    active: bool = True


@dataclass(frozen=True, slots=True)
class CommunicationDeliveryRecord:
    id: str
    message_id: str
    provider: str
    status: str
    attempt_count: int
    attempted_at: datetime | None = None
    sent_at: datetime | None = None
    provider_message_id: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    last_actor: str | None = None


@dataclass(frozen=True, slots=True)
class CommunicationMessageRecord:
    id: str
    audience: str
    recipient_id: str
    recipient_email: str | None
    subject: str
    body: str
    included: bool
    message_key: str | None = None
    project_id: str | None = None
    cc_emails: tuple[str, ...] = ()
    to_emails: tuple[str, ...] = ()
    content_fingerprint: str | None = None
    approvable: bool = True
    diagnostics_json: str | None = None
    deliveries: tuple[CommunicationDeliveryRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class CommunicationBatchRecord:
    id: str
    week_start: date
    kind: str
    snapshot_fingerprint: str
    status: str
    prepared_by: str | None
    prepared_at: datetime
    approved_by: str | None = None
    approved_at: datetime | None = None
    communicated_by: str | None = None
    communicated_at: datetime | None = None
    cancelled_by: str | None = None
    cancelled_at: datetime | None = None
    drafts_provider: str | None = None
    drafts_created_count: int = 0
    drafts_created_by: str | None = None
    drafts_created_at: datetime | None = None
    messages: tuple[CommunicationMessageRecord, ...] = ()
    stale: bool = False
    model_version: str = "legacy"


@dataclass(frozen=True, slots=True)
class CommunicationPreview:
    week_start: date
    mode: str
    snapshot_fingerprint: str
    drafts: tuple[CommunicationDraft, ...]
    missing_contact_ids: tuple[str, ...]
    has_communicated_baseline: bool
    baseline_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class CommunicationReviewInput:
    audience: str
    recipient_id: str
    include: bool = True
    subject: str | None = None
    body: str | None = None


@dataclass(frozen=True, slots=True)
class CommunicationTransportMessage:
    audience: str
    recipient_id: str
    recipient_email: str
    subject: str
    body: str
    cc_emails: tuple[str, ...] = ()
    to_emails: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CommunicationTransportResult:
    provider: str
    created_count: int


class CommunicationTransportPort(Protocol):
    def create_drafts(
        self,
        messages: Sequence[CommunicationTransportMessage],
    ) -> CommunicationTransportResult: ...


class NoopCommunicationTransport:
    """Local/test adapter. It never sends or creates external messages."""

    def __init__(self) -> None:
        self.captured: list[CommunicationTransportMessage] = []

    def create_drafts(
        self,
        messages: Sequence[CommunicationTransportMessage],
    ) -> CommunicationTransportResult:
        self.captured.extend(messages)
        return CommunicationTransportResult(provider="noop", created_count=0)


class CommunicationRepositoryPort(Protocol):
    def synchronize_known_contacts(self) -> None: ...
    def list_contacts(self) -> Sequence[CommunicationContactRecord]: ...
    def upsert_contact(
        self,
        *,
        recipient_id: str,
        audience: str,
        display_name: str,
        email: str | None,
        active: bool,
    ) -> CommunicationContactRecord: ...
    def weekly_assignments(self, *, week_start: date) -> Sequence[WeeklyAssignment]: ...
    def weekly_technician_ids(self) -> Sequence[str]: ...
    def latest_communicated_snapshot(
        self,
        *,
        week_start: date,
    ) -> tuple[str, Sequence[WeeklyAssignment]] | None: ...
    def create_prepared_batch(
        self,
        *,
        week_start: date,
        kind: str,
        fingerprint: str,
        actor_name: str,
        messages: Sequence[tuple[CommunicationDraft, bool]],
        snapshot: Sequence[WeeklyAssignment],
    ) -> CommunicationBatchRecord: ...
    def list_batches(self, *, week_start: date | None = None) -> Sequence[CommunicationBatchRecord]: ...
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
    def has_duplicate_batch(
        self,
        *,
        week_start: date,
        kind: str,
        fingerprint: str,
    ) -> bool: ...


class CommunicationService:
    def __init__(
        self,
        repository: CommunicationRepositoryPort,
        transport: CommunicationTransportPort | None = None,
    ) -> None:
        self._repository = repository
        self._transport = transport

    @staticmethod
    def _normalize_week_start(value: date) -> date:
        return value - timedelta(days=value.weekday())

    def list_contacts(self) -> tuple[CommunicationContactRecord, ...]:
        self._repository.synchronize_known_contacts()
        return tuple(self._repository.list_contacts())

    def update_contact(
        self,
        *,
        recipient_id: str,
        audience: str,
        display_name: str,
        email: str | None,
        active: bool,
    ) -> CommunicationContactRecord:
        recipient = str(recipient_id or "").strip()
        audience_value = str(audience or "").strip()
        display = str(display_name or "").strip()
        email_value = str(email or "").strip() or None
        if not recipient or audience_value not in {"technician", "project_manager"} or not display:
            raise ApplicationValidationError(
                "Le contact est invalide.",
                code="communication_contact_invalid",
            )
        return self._repository.upsert_contact(
            recipient_id=recipient,
            audience=audience_value,
            display_name=display,
            email=email_value,
            active=bool(active),
        )

    def preview(self, *, week_start: date) -> CommunicationPreview:
        week = self._normalize_week_start(week_start)
        self._repository.synchronize_known_contacts()
        assignments = tuple(self._repository.weekly_assignments(week_start=week))
        current_fingerprint = snapshot_fingerprint(assignments)
        contacts = {
            row.recipient_id: Contact(
                person_id=row.recipient_id,
                display_name=row.display_name,
                email=row.email or "",
            )
            for row in self._repository.list_contacts()
            if row.active
        }
        baseline = self._repository.latest_communicated_snapshot(week_start=week)
        if baseline is None:
            batch = build_weekly_plan_batch(
                assignments,
                contacts,
                week,
                technician_ids=tuple(self._repository.weekly_technician_ids()),
            )
            mode = KIND_WEEKLY
            baseline_fingerprint = None
        else:
            baseline_fingerprint, previous = baseline
            if baseline_fingerprint == current_fingerprint:
                batch = CommunicationBatch((), (), current_fingerprint)
            else:
                batch = build_change_notification_batch(previous, assignments, contacts, week)
            mode = KIND_CHANGE
        return CommunicationPreview(
            week_start=week,
            mode=mode,
            snapshot_fingerprint=current_fingerprint,
            drafts=tuple(batch.drafts),
            missing_contact_ids=tuple(batch.missing_contact_ids),
            has_communicated_baseline=baseline is not None,
            baseline_fingerprint=baseline[0] if baseline is not None else None,
        )

    def prepare(
        self,
        *,
        week_start: date,
        expected_fingerprint: str,
        reviews: Sequence[CommunicationReviewInput],
        actor_name: str,
    ) -> CommunicationBatchRecord:
        preview = self.preview(week_start=week_start)
        if str(expected_fingerprint or "").strip() != preview.snapshot_fingerprint:
            raise ApplicationConflictError(
                "Le planning a changé depuis la prévisualisation.",
                code="communication_preview_stale",
            )
        if preview.missing_contact_ids:
            raise ApplicationValidationError(
                "Des coordonnées de communication sont manquantes.",
                code="communication_contacts_missing",
                context={"recipient_ids": list(preview.missing_contact_ids)},
            )
        if not preview.drafts:
            raise ApplicationValidationError(
                "Aucun message n'est requis pour cette version du planning.",
                code="communication_no_messages",
            )
        review_map = {
            (row.audience, row.recipient_id): DraftReview(
                include=row.include,
                subject=row.subject,
                body=row.body,
            )
            for row in reviews
        }
        reviewed = apply_manual_review(
            CommunicationBatch(
                preview.drafts,
                preview.missing_contact_ids,
                preview.snapshot_fingerprint,
            ),
            review_map,
        )
        if not reviewed.drafts:
            raise ApplicationValidationError(
                "Au moins un message doit rester inclus.",
                code="communication_no_included_messages",
            )
        if self._repository.has_duplicate_batch(
            week_start=preview.week_start,
            kind=preview.mode,
            fingerprint=preview.snapshot_fingerprint,
        ):
            raise ApplicationConflictError(
                "Un lot existe déjà pour cette version du planning.",
                code="communication_batch_duplicate",
            )
        reviewed_by_key = {(row.audience, row.recipient_id): row for row in reviewed.drafts}
        stored_messages: list[tuple[CommunicationDraft, bool]] = []
        for original in preview.drafts:
            key = (original.audience, original.recipient_id)
            included = reviewed_by_key.get(key)
            if included is not None:
                stored_messages.append((included, True))
            else:
                review = review_map.get(key, DraftReview())
                stored_messages.append(
                    (
                        CommunicationDraft(
                            audience=original.audience,
                            recipient_id=original.recipient_id,
                            recipient_email=original.recipient_email,
                            subject=(review.subject if review.subject is not None else original.subject),
                            body=(review.body if review.body is not None else original.body),
                            message_kind=original.message_kind,
                            week_start=original.week_start,
                            snapshot_fingerprint=original.snapshot_fingerprint,
                            requires_manual_approval=True,
                        ),
                        False,
                    )
                )
        assignments = tuple(self._repository.weekly_assignments(week_start=preview.week_start))
        return self._repository.create_prepared_batch(
            week_start=preview.week_start,
            kind=preview.mode,
            fingerprint=preview.snapshot_fingerprint,
            actor_name=str(actor_name or "").strip() or "api",
            messages=stored_messages,
            snapshot=assignments,
        )

    def list_batches(self, *, week_start: date | None = None) -> tuple[CommunicationBatchRecord, ...]:
        week = self._normalize_week_start(week_start) if week_start else None
        rows = tuple(self._repository.list_batches(week_start=week))
        if week is None:
            return rows
        current_fingerprint = snapshot_fingerprint(
            self._repository.weekly_assignments(week_start=week)
        )
        return tuple(
            CommunicationBatchRecord(
                **{
                    **{field: getattr(row, field) for field in row.__dataclass_fields__ if field != "stale"},
                    "stale": row.status in {STATUS_PREPARED, STATUS_APPROVED}
                    and row.snapshot_fingerprint != current_fingerprint,
                }
            )
            for row in rows
        )

    def _batch(self, batch_id: str) -> CommunicationBatchRecord:
        row = self._repository.get_batch(str(batch_id or "").strip())
        if row is None or row.model_version != "legacy":
            raise ApplicationNotFoundError(
                "Lot de communication introuvable.",
                code="communication_batch_not_found",
                context={"batch_id": batch_id},
            )
        return row

    def _assert_current_snapshot(self, row: CommunicationBatchRecord, *, message: str) -> None:
        current = snapshot_fingerprint(self._repository.weekly_assignments(week_start=row.week_start))
        if current != row.snapshot_fingerprint:
            raise ApplicationConflictError(
                message,
                code="communication_batch_stale",
            )

    def approve(self, *, batch_id: str, actor_name: str) -> CommunicationBatchRecord:
        row = self._batch(batch_id)
        if row.status != STATUS_PREPARED:
            raise ApplicationConflictError(
                "Seul un lot préparé peut être approuvé.",
                code="communication_batch_not_prepared",
            )
        self._assert_current_snapshot(
            row,
            message="Le planning a changé depuis la préparation du lot.",
        )
        return self._repository.set_batch_status(
            batch_id=row.id,
            status=STATUS_APPROVED,
            actor_name=actor_name,
        )

    def create_drafts(self, *, batch_id: str, actor_name: str) -> CommunicationBatchRecord:
        row = self._batch(batch_id)
        if row.status != STATUS_APPROVED:
            raise ApplicationConflictError(
                "Le lot doit être approuvé avant de créer les brouillons M365.",
                code="communication_batch_not_approved",
            )
        if row.drafts_created_at is not None:
            raise ApplicationConflictError(
                "Les brouillons M365 ont déjà été créés pour ce lot.",
                code="communication_drafts_already_created",
            )
        self._assert_current_snapshot(
            row,
            message="Le planning a changé depuis l'approbation; préparez un nouveau lot.",
        )
        if self._transport is None:
            raise ApplicationUnavailableError(
                "Le transport Microsoft 365 n'est pas configuré sur ce serveur.",
                code="communication_transport_unavailable",
            )
        messages = tuple(
            CommunicationTransportMessage(
                audience=message.audience,
                recipient_id=message.recipient_id,
                recipient_email=message.recipient_email,
                subject=message.subject,
                body=message.body,
                cc_emails=message.cc_emails,
            )
            for message in row.messages
            if message.included
        )
        if not messages:
            raise ApplicationValidationError(
                "Aucun message inclus n'est disponible pour la création de brouillons.",
                code="communication_no_included_messages",
            )
        result = self._transport.create_drafts(messages)
        if result.created_count != len(messages):
            raise ApplicationUnavailableError(
                "Le transport M365 n'a pas confirmé la création de tous les brouillons.",
                code="communication_transport_incomplete",
                context={
                    "expected_count": len(messages),
                    "created_count": result.created_count,
                    "provider": result.provider,
                },
            )
        return self._repository.mark_drafts_created(
            batch_id=row.id,
            provider=result.provider,
            created_count=result.created_count,
            actor_name=actor_name,
        )

    def cancel(self, *, batch_id: str, actor_name: str) -> CommunicationBatchRecord:
        row = self._batch(batch_id)
        if row.status not in {STATUS_PREPARED, STATUS_APPROVED}:
            raise ApplicationConflictError(
                "Ce lot ne peut plus être annulé.",
                code="communication_batch_not_cancellable",
            )
        return self._repository.set_batch_status(
            batch_id=row.id,
            status=STATUS_CANCELLED,
            actor_name=actor_name,
        )

    def mark_communicated(self, *, batch_id: str, actor_name: str) -> CommunicationBatchRecord:
        row = self._batch(batch_id)
        if row.status != STATUS_APPROVED:
            raise ApplicationConflictError(
                "Le lot doit être approuvé avant d'être confirmé comme communiqué.",
                code="communication_batch_not_approved",
            )
        self._assert_current_snapshot(
            row,
            message="Le planning a changé depuis l'approbation; préparez un nouveau lot.",
        )
        return self._repository.set_batch_status(
            batch_id=row.id,
            status=STATUS_COMMUNICATED,
            actor_name=actor_name,
        )
