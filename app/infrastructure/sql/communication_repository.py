from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import json
from typing import Sequence

from sqlalchemy import select, update, true
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...application.project_communications import MODEL_VERSION_PROJECT_V2
from ...application.communications import (
    DELIVERY_PROVIDER_SMTP,
    DELIVERY_STATUS_FAILED,
    DELIVERY_STATUS_PENDING,
    DELIVERY_STATUS_SENDING,
    DELIVERY_STATUS_SENT,
    KIND_CHANGE,
    KIND_WEEKLY,
    STATUS_APPROVED,
    STATUS_CANCELLED,
    STATUS_COMMUNICATED,
    STATUS_PREPARED,
    CommunicationBatchRecord,
    CommunicationContactRecord,
    CommunicationDeliveryRecord,
    CommunicationMessageRecord,
    CommunicationRepositoryPort,
)
from ...domain.communication_planning import CommunicationDraft, WeeklyAssignment
from ...domain.project_communication import ProjectCommunicationProjection
from ...domain.project_communication_messages import (
    ProjectCommunicationDraft,
    deserialize_project_projection,
    serialize_project_projection,
)
from ...domain.confirmation import effective_confirmation
from .base import utc_now
from .communication_models import (
    CommunicationBatchRow,
    CommunicationContact,
    CommunicationDeliveryRow,
    CommunicationMessageRow,
    CommunicationSnapshotLine,
)
from .models import Project, Resource, ResourceRequirement, Shift, WorkforceRequest


def _text(value: object) -> str:
    return str(value or "").strip()


def _optional_text(value: object) -> str | None:
    value = _text(value)
    return value or None


def _technician_id(resource_id: str) -> str:
    return f"resource:{resource_id}"


def _manager_id(external_id: str | None) -> str:
    value = _text(external_id)
    return f"pm:{value}" if value else ""


class SqlCommunicationRepository(CommunicationRepositoryPort):
    def __init__(self, session: Session) -> None:
        self._session = session

    @staticmethod
    def _contact(row: CommunicationContact) -> CommunicationContactRecord:
        return CommunicationContactRecord(
            recipient_id=row.recipient_id,
            audience=row.audience,
            display_name=row.display_name,
            email=_optional_text(row.email),
            active=bool(row.active),
        )

    def _insert_synchronized_contact(
        self,
        *,
        recipient_id: str,
        audience: str,
        display_name: str,
        email: str | None,
        active: bool,
    ) -> CommunicationContact:
        candidate = CommunicationContact(
            recipient_id=recipient_id,
            audience=audience,
            display_name=display_name,
            email=email,
            active=active,
        )
        try:
            with self._session.begin_nested():
                self._session.add(candidate)
                self._session.flush([candidate])
            return candidate
        except IntegrityError:
            row = self._session.scalar(
                select(CommunicationContact).where(
                    CommunicationContact.recipient_id == recipient_id
                )
            )
            if row is None:
                raise
            return row

    def synchronize_known_contacts(self) -> None:
        existing = {
            row.recipient_id: row
            for row in self._session.scalars(select(CommunicationContact)).all()
        }
        resources = self._session.scalars(select(Resource)).all()
        for resource in resources:
            recipient_id = _technician_id(resource.id)
            row = existing.get(recipient_id)
            if row is None:
                row = self._insert_synchronized_contact(
                    recipient_id=recipient_id,
                    audience="technician",
                    display_name=resource.name,
                    email=_optional_text(resource.email),
                    active=bool(resource.active),
                )
                existing[recipient_id] = row
            else:
                row.display_name = resource.name
                row.active = bool(resource.active)

        managers: dict[str, str] = {}
        for project in self._session.scalars(select(Project)).all():
            external_id = _text(project.project_manager_external_id)
            if external_id:
                managers[external_id] = _text(project.project_manager_name) or external_id
        for external_id, display_name in managers.items():
            recipient_id = _manager_id(external_id)
            row = existing.get(recipient_id)
            if row is None:
                row = self._insert_synchronized_contact(
                    recipient_id=recipient_id,
                    audience="project_manager",
                    display_name=display_name,
                    email=None,
                    active=True,
                )
                existing[recipient_id] = row
            else:
                row.display_name = display_name
        self._session.flush()

    def list_contacts(self) -> tuple[CommunicationContactRecord, ...]:
        rows = self._session.scalars(
            select(CommunicationContact).order_by(
                CommunicationContact.audience,
                CommunicationContact.display_name,
                CommunicationContact.recipient_id,
            )
        ).all()
        return tuple(self._contact(row) for row in rows)

    def upsert_contact(
        self,
        *,
        recipient_id: str,
        audience: str,
        display_name: str,
        email: str | None,
        active: bool,
    ) -> CommunicationContactRecord:
        row = self._session.scalar(
            select(CommunicationContact).where(CommunicationContact.recipient_id == recipient_id)
        )
        if row is None:
            row = CommunicationContact(
                recipient_id=recipient_id,
                audience=audience,
                display_name=display_name,
                email=email,
                active=active,
            )
            self._session.add(row)
        else:
            row.audience = audience
            row.display_name = display_name
            row.email = email
            row.active = active
        self._session.flush()
        return self._contact(row)

    def weekly_assignments(self, *, week_start: date) -> tuple[WeeklyAssignment, ...]:
        week_end = week_start + timedelta(days=6)
        rows = self._session.execute(
            select(Shift, ResourceRequirement, Resource, Project, WorkforceRequest)
            .join(ResourceRequirement, Shift.resource_requirement_id == ResourceRequirement.id)
            .join(Resource, Shift.resource_id == Resource.id)
            .join(Project, ResourceRequirement.project_id == Project.id)
            .outerjoin(WorkforceRequest, ResourceRequirement.workforce_request_id == WorkforceRequest.id)
            .where(Shift.work_date >= week_start, Shift.work_date <= week_end)
            .order_by(Shift.work_date, Resource.name, Project.number, Shift.id)
        ).all()
        result: list[WeeklyAssignment] = []
        for shift, requirement, resource, project, _request in rows:
            result.append(
                WeeklyAssignment(
                    segment_id=_text(requirement.legacy_segment_id) or requirement.id,
                    resource_id=_technician_id(resource.id),
                    resource_name=resource.name,
                    project_manager_id=_manager_id(project.project_manager_external_id),
                    project_number=_text(project.number),
                    project_name=_text(project.name),
                    day=shift.work_date,
                    hours=float(shift.hours),
                    allocation_type=_text(shift.allocation_type) or _text(requirement.planning_type) or "Flexible",
                    outside_schedule=bool(shift.outside_standard_hours),
                    confirmation=effective_confirmation(shift.confirmation, requirement.confirmation),
                )
            )
        return tuple(result)

    def weekly_technician_ids(self) -> tuple[str, ...]:
        rows = self._session.scalars(
            select(Resource).where(Resource.active == true()).order_by(Resource.sort_order, Resource.name)
        ).all()
        return tuple(_technician_id(row.id) for row in rows)

    def _deliveries(self, message_id: str) -> tuple[CommunicationDeliveryRecord, ...]:
        rows = self._session.scalars(
            select(CommunicationDeliveryRow)
            .where(CommunicationDeliveryRow.message_id == message_id)
            .order_by(CommunicationDeliveryRow.provider, CommunicationDeliveryRow.id)
        ).all()
        return tuple(
            CommunicationDeliveryRecord(
                id=row.id,
                message_id=row.message_id,
                provider=row.provider,
                status=row.status,
                attempt_count=int(row.attempt_count or 0),
                attempted_at=row.attempted_at,
                sent_at=row.sent_at,
                provider_message_id=_optional_text(row.provider_message_id),
                error_code=_optional_text(row.error_code),
                error_detail=_optional_text(row.error_detail),
                last_actor=_optional_text(row.last_actor),
            )
            for row in rows
        )

    def _messages(self, batch_id: str) -> tuple[CommunicationMessageRecord, ...]:
        rows = self._session.scalars(
            select(CommunicationMessageRow)
            .where(CommunicationMessageRow.batch_id == batch_id)
            .order_by(CommunicationMessageRow.audience, CommunicationMessageRow.recipient_id)
        ).all()
        result: list[CommunicationMessageRecord] = []
        for row in rows:
            cc_emails: tuple[str, ...] = ()
            if row.cc_recipients_json:
                payload = json.loads(row.cc_recipients_json)
                cc_emails = tuple(
                    str(item.get("email") or "").strip()
                    for item in payload
                    if isinstance(item, dict)
                    and str(item.get("email") or "").strip()
                )
            result.append(
                CommunicationMessageRecord(
                    id=row.id,
                    audience=row.audience,
                    recipient_id=row.recipient_id,
                    recipient_email=_optional_text(row.recipient_email),
                    subject=row.subject,
                    body=row.body,
                    included=bool(row.included),
                    message_key=_optional_text(row.message_key),
                    project_id=_optional_text(row.project_id),
                    cc_emails=cc_emails,
                    content_fingerprint=_optional_text(row.content_fingerprint),
                    approvable=bool(row.approvable),
                    diagnostics_json=_optional_text(row.diagnostics_json),
                    deliveries=self._deliveries(row.id),
                )
            )
        return tuple(result)

    def _batch(self, row: CommunicationBatchRow) -> CommunicationBatchRecord:
        return CommunicationBatchRecord(
            id=row.id,
            week_start=row.week_start,
            kind=row.kind,
            snapshot_fingerprint=row.snapshot_fingerprint,
            status=row.status,
            prepared_by=_optional_text(row.prepared_by),
            prepared_at=row.prepared_at,
            approved_by=_optional_text(row.approved_by),
            approved_at=row.approved_at,
            communicated_by=_optional_text(row.communicated_by),
            communicated_at=row.communicated_at,
            cancelled_by=_optional_text(row.cancelled_by),
            cancelled_at=row.cancelled_at,
            drafts_provider=_optional_text(row.drafts_provider),
            drafts_created_count=int(row.drafts_created_count or 0),
            drafts_created_by=_optional_text(row.drafts_created_by),
            drafts_created_at=row.drafts_created_at,
            messages=self._messages(row.id),
            model_version=_text(row.model_version) or "legacy",
        )

    def latest_communicated_snapshot(
        self,
        *,
        week_start: date,
    ) -> tuple[str, tuple[WeeklyAssignment, ...]] | None:
        batch = self._session.scalar(
            select(CommunicationBatchRow)
            .where(
                CommunicationBatchRow.week_start == week_start,
                CommunicationBatchRow.status == STATUS_COMMUNICATED,
                CommunicationBatchRow.model_version == "legacy",
            )
            .order_by(CommunicationBatchRow.communicated_at.desc(), CommunicationBatchRow.id.desc())
        )
        if batch is None:
            return None
        rows = self._session.scalars(
            select(CommunicationSnapshotLine)
            .where(CommunicationSnapshotLine.batch_id == batch.id)
            .order_by(
                CommunicationSnapshotLine.day,
                CommunicationSnapshotLine.resource_id,
                CommunicationSnapshotLine.segment_id,
            )
        ).all()
        return (
            batch.snapshot_fingerprint,
            tuple(
                WeeklyAssignment(
                    segment_id=row.segment_id,
                    resource_id=row.resource_id,
                    resource_name=row.resource_name,
                    project_manager_id=row.project_manager_id,
                    project_number=row.project_number,
                    project_name=row.project_name,
                    day=row.day,
                    hours=float(row.hours),
                    allocation_type=row.allocation_type,
                    outside_schedule=bool(row.outside_schedule),
                    confirmation=row.confirmation,
                )
                for row in rows
            ),
        )

    def latest_communicated_project_snapshot(
        self,
        *,
        week_start: date,
    ) -> tuple[str, ProjectCommunicationProjection] | None:
        batch = self._session.scalar(
            select(CommunicationBatchRow)
            .where(
                CommunicationBatchRow.week_start == week_start,
                CommunicationBatchRow.status == STATUS_COMMUNICATED,
                CommunicationBatchRow.model_version == MODEL_VERSION_PROJECT_V2,
                CommunicationBatchRow.project_snapshot_json.is_not(None),
            )
            .order_by(
                CommunicationBatchRow.communicated_at.desc(),
                CommunicationBatchRow.id.desc(),
            )
        )
        if batch is None or not batch.project_snapshot_json:
            return None
        return (
            batch.snapshot_fingerprint,
            deserialize_project_projection(batch.project_snapshot_json),
        )

    def create_project_prepared_batch(
        self,
        *,
        week_start: date,
        kind: str,
        fingerprint: str,
        actor_name: str,
        messages: Sequence[tuple[ProjectCommunicationDraft, bool]],
        snapshot: ProjectCommunicationProjection,
    ) -> CommunicationBatchRecord:
        if kind not in {"project_confirmation", "project_planning_change"}:
            raise ValueError(f"Type de lot projet invalide: {kind}")
        batch = CommunicationBatchRow(
            week_start=week_start,
            kind=kind,
            snapshot_fingerprint=fingerprint,
            status=STATUS_PREPARED,
            prepared_by=_optional_text(actor_name),
            prepared_at=utc_now(),
            model_version=MODEL_VERSION_PROJECT_V2,
            project_snapshot_json=serialize_project_projection(snapshot),
        )
        self._session.add(batch)
        self._session.flush()

        for draft, included in messages:
            cc_payload = [
                {
                    "contact_id": participant.contact_id,
                    "user_id": participant.user_id,
                    "display_name": participant.display_name,
                    "email": participant.email,
                }
                for participant in draft.cc_recipients
            ]
            diagnostics_payload = [
                {
                    "code": diagnostic.code,
                    "severity": diagnostic.severity,
                    "entity_type": diagnostic.entity_type,
                    "entity_id": diagnostic.entity_id,
                    "message": diagnostic.message,
                }
                for diagnostic in draft.diagnostics
            ]
            recipient_id = (
                draft.to_recipient.contact_id
                or draft.to_recipient.user_id
                or draft.project_id
            )
            message_row = CommunicationMessageRow(
                batch_id=batch.id,
                audience=draft.audience,
                recipient_id=recipient_id,
                recipient_email=_optional_text(draft.to_recipient.email),
                message_key=draft.message_key,
                project_id=draft.project_id,
                cc_recipients_json=json.dumps(
                    cc_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                content_fingerprint=draft.content_fingerprint,
                approvable=bool(draft.approvable),
                diagnostics_json=json.dumps(
                    diagnostics_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                subject=draft.subject,
                body=draft.body,
                included=bool(included),
            )
            self._session.add(message_row)
            self._session.flush()
            if included:
                self._session.add(
                    CommunicationDeliveryRow(
                        batch_id=batch.id,
                        message_id=message_row.id,
                        provider=DELIVERY_PROVIDER_SMTP,
                        status=DELIVERY_STATUS_PENDING,
                    )
                )
        self._session.flush()
        return self._batch(batch)

    def claim_smtp_delivery(
        self,
        *,
        message_id: str,
        actor_name: str,
    ) -> CommunicationDeliveryRecord | None:
        now = utc_now()
        result = self._session.execute(
            update(CommunicationDeliveryRow)
            .where(
                CommunicationDeliveryRow.message_id == message_id,
                CommunicationDeliveryRow.provider == DELIVERY_PROVIDER_SMTP,
                CommunicationDeliveryRow.status.in_(
                    (DELIVERY_STATUS_PENDING, DELIVERY_STATUS_FAILED)
                ),
            )
            .values(
                status=DELIVERY_STATUS_SENDING,
                attempt_count=CommunicationDeliveryRow.attempt_count + 1,
                attempted_at=now,
                error_code=None,
                error_detail=None,
                last_actor=_optional_text(actor_name),
            )
        )
        self._session.flush()
        if int(result.rowcount or 0) != 1:
            return None
        row = self._session.scalar(
            select(CommunicationDeliveryRow).where(
                CommunicationDeliveryRow.message_id == message_id,
                CommunicationDeliveryRow.provider == DELIVERY_PROVIDER_SMTP,
            )
        )
        if row is None:
            return None
        return self._deliveries(message_id)[0]

    def complete_smtp_delivery(
        self,
        *,
        delivery_id: str,
        provider_message_id: str,
        actor_name: str,
    ) -> CommunicationDeliveryRecord:
        row = self._session.get(CommunicationDeliveryRow, delivery_id)
        if row is None:
            raise KeyError(delivery_id)
        row.status = DELIVERY_STATUS_SENT
        row.sent_at = utc_now()
        row.provider_message_id = _optional_text(provider_message_id)
        row.error_code = None
        row.error_detail = None
        row.last_actor = _optional_text(actor_name)
        self._session.flush()
        return self._deliveries(row.message_id)[0]

    def fail_smtp_delivery(
        self,
        *,
        delivery_id: str,
        error_code: str,
        error_detail: str,
        actor_name: str,
    ) -> CommunicationDeliveryRecord:
        row = self._session.get(CommunicationDeliveryRow, delivery_id)
        if row is None:
            raise KeyError(delivery_id)
        row.status = DELIVERY_STATUS_FAILED
        row.error_code = _optional_text(error_code)
        row.error_detail = _optional_text(error_detail)
        row.last_actor = _optional_text(actor_name)
        self._session.flush()
        return self._deliveries(row.message_id)[0]

    def list_project_batches(
        self,
        *,
        week_start: date | None = None,
    ) -> tuple[CommunicationBatchRecord, ...]:
        statement = select(CommunicationBatchRow).where(
            CommunicationBatchRow.model_version == MODEL_VERSION_PROJECT_V2
        )
        if week_start is not None:
            statement = statement.where(
                CommunicationBatchRow.week_start == week_start
            )
        rows = self._session.scalars(
            statement.order_by(
                CommunicationBatchRow.prepared_at.desc(),
                CommunicationBatchRow.id.desc(),
            )
        ).all()
        return tuple(self._batch(row) for row in rows)

    def has_duplicate_batch(
        self,
        *,
        week_start: date,
        kind: str,
        fingerprint: str,
    ) -> bool:
        return self._session.scalar(
            select(CommunicationBatchRow.id).where(
                CommunicationBatchRow.week_start == week_start,
                CommunicationBatchRow.kind == kind,
                CommunicationBatchRow.snapshot_fingerprint == fingerprint,
                CommunicationBatchRow.status.in_((STATUS_PREPARED, STATUS_APPROVED, STATUS_COMMUNICATED)),
            )
        ) is not None

    def create_prepared_batch(
        self,
        *,
        week_start: date,
        kind: str,
        fingerprint: str,
        actor_name: str,
        messages: Sequence[tuple[CommunicationDraft, bool]],
        snapshot: Sequence[WeeklyAssignment],
    ) -> CommunicationBatchRecord:
        if kind not in {KIND_WEEKLY, KIND_CHANGE}:
            raise ValueError(f"Type de lot invalide: {kind}")
        now = utc_now()
        batch = CommunicationBatchRow(
            week_start=week_start,
            kind=kind,
            snapshot_fingerprint=fingerprint,
            status=STATUS_PREPARED,
            prepared_by=_optional_text(actor_name),
            prepared_at=now,
            model_version="legacy",
        )
        self._session.add(batch)
        self._session.flush()
        for draft, included in messages:
            self._session.add(
                CommunicationMessageRow(
                    batch_id=batch.id,
                    audience=draft.audience,
                    recipient_id=draft.recipient_id,
                    recipient_email=draft.recipient_email,
                    subject=draft.subject,
                    body=draft.body,
                    included=bool(included),
                )
            )
        for assignment in snapshot:
            self._session.add(
                CommunicationSnapshotLine(
                    batch_id=batch.id,
                    segment_id=assignment.segment_id,
                    resource_id=assignment.resource_id,
                    resource_name=assignment.resource_name,
                    project_manager_id=assignment.project_manager_id,
                    project_number=assignment.project_number,
                    project_name=assignment.project_name,
                    day=assignment.day,
                    hours=Decimal(str(assignment.hours)),
                    allocation_type=assignment.allocation_type,
                    outside_schedule=bool(assignment.outside_schedule),
                    confirmation=assignment.confirmation,
                )
            )
        self._session.flush()
        return self._batch(batch)

    def list_batches(self, *, week_start: date | None = None) -> tuple[CommunicationBatchRecord, ...]:
        statement = select(CommunicationBatchRow).where(
            CommunicationBatchRow.model_version == "legacy"
        )
        if week_start is not None:
            statement = statement.where(CommunicationBatchRow.week_start == week_start)
        rows = self._session.scalars(
            statement.order_by(CommunicationBatchRow.prepared_at.desc(), CommunicationBatchRow.id.desc())
        ).all()
        return tuple(self._batch(row) for row in rows)

    def get_batch(self, batch_id: str) -> CommunicationBatchRecord | None:
        row = self._session.get(CommunicationBatchRow, batch_id)
        return self._batch(row) if row is not None else None

    def set_batch_status(
        self,
        *,
        batch_id: str,
        status: str,
        actor_name: str,
    ) -> CommunicationBatchRecord:
        row = self._session.get(CommunicationBatchRow, batch_id)
        if row is None:
            raise KeyError(batch_id)
        now = utc_now()
        actor = _optional_text(actor_name)
        row.status = status
        if status == STATUS_APPROVED:
            row.approved_by = actor
            row.approved_at = now
        elif status == STATUS_COMMUNICATED:
            row.communicated_by = actor
            row.communicated_at = now
        elif status == STATUS_CANCELLED:
            row.cancelled_by = actor
            row.cancelled_at = now
        self._session.flush()
        return self._batch(row)

    def mark_drafts_created(
        self,
        *,
        batch_id: str,
        provider: str,
        created_count: int,
        actor_name: str,
    ) -> CommunicationBatchRecord:
        row = self._session.get(CommunicationBatchRow, batch_id)
        if row is None:
            raise KeyError(batch_id)
        row.drafts_provider = _optional_text(provider)
        row.drafts_created_count = max(0, int(created_count))
        row.drafts_created_by = _optional_text(actor_name)
        row.drafts_created_at = utc_now()
        self._session.flush()
        return self._batch(row)
