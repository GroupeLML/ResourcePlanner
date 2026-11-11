from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...domain.approval_envelope import (
    DECISION_APPROVAL_REFERENCE_UNKNOWN,
    EnvelopeDecision,
    approval_envelope_from_snapshot_payload,
    compare_approval_envelopes,
)
from .approval_revision_models import (
    APPROVAL_REFERENCE_CAPTURED,
    RequestApprovalReference,
    RequestApprovalRevision,
)
from .approval_revision_repository import SqlRequestApprovalRevisionRepository
from .base import utc_now
from .models import WorkforceRequest, WorkforceRequestHistory


def _text(value: object) -> str:
    return str(value or "").strip()


class SqlDemandApprovalEnvelopePolicyRepository:
    """Evaluate the persisted candidate against the active immutable authorization."""

    def __init__(self, session: Session, *, actor_name: str = "") -> None:
        self._session = session
        self._actor_name = _text(actor_name)
        self._revisions = SqlRequestApprovalRevisionRepository(session)

    def _request(self, demand_number: str) -> WorkforceRequest:
        wanted = _text(demand_number)
        request = self._session.scalar(
            select(WorkforceRequest).where(
                (WorkforceRequest.id == wanted)
                | (WorkforceRequest.legacy_demand_number == wanted)
            )
        )
        if request is None:
            raise KeyError(f"Demande {wanted} introuvable")
        return request

    def evaluate_candidate(
        self,
        demand_number: str,
        *,
        actor_role: str | None = None,
    ) -> EnvelopeDecision:
        request = self._request(demand_number)
        candidate = self._revisions.candidate_envelope(request)
        reference = self._session.get(RequestApprovalReference, request.id)
        if (
            reference is None
            or reference.status != APPROVAL_REFERENCE_CAPTURED
            or not reference.active_revision_id
        ):
            return compare_approval_envelopes(
                None,
                candidate,
                actor_role=actor_role,
                approval_reference_known=False,
            )

        revision = self._session.get(
            RequestApprovalRevision,
            reference.active_revision_id,
        )
        if revision is None:
            return compare_approval_envelopes(
                None,
                candidate,
                actor_role=actor_role,
                approval_reference_known=False,
            )
        payload = json.loads(revision.payload_text)
        authorization = payload.get("authorization")
        if not isinstance(authorization, dict):
            return EnvelopeDecision(
                decision=DECISION_APPROVAL_REFERENCE_UNKNOWN,
                reason=DECISION_APPROVAL_REFERENCE_UNKNOWN,
            )
        approved = approval_envelope_from_snapshot_payload(authorization)
        return compare_approval_envelopes(
            approved,
            candidate,
            actor_role=actor_role,
            approval_reference_known=True,
        )

    def candidate_confirmations(
        self,
        demand_number: str,
    ) -> dict[str, str]:
        request = self._request(demand_number)
        candidate = self._revisions.candidate_envelope(request)
        return {
            entry.identity.stable_key: entry.confirmation
            for entry in candidate.entries
        }

    def _append_history(
        self,
        request: WorkforceRequest,
        *,
        action: str,
        comment: str,
        decision: EnvelopeDecision,
        previous_status: str | None = None,
    ) -> None:
        self._session.add(
            WorkforceRequestHistory(
                workforce_request_id=request.id,
                action=action,
                previous_status=previous_status,
                status=request.status,
                comment=comment,
                details=json.dumps(
                    {
                        "aggregate_version": int(request.aggregate_version or 1),
                        "envelope_decision": decision.to_dict(),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                actor_name=self._actor_name or None,
                occurred_at=utc_now(),
            )
        )

    def record_candidate_decision(
        self,
        demand_number: str,
        decision: EnvelopeDecision,
    ) -> None:
        request = self._request(demand_number)
        self._append_history(
            request,
            action="Comparaison enveloppe approuvée",
            comment=(
                "La proposition candidate a été comparée à la révision approuvée active "
                f"({decision.decision} · {decision.reason})."
            ),
            decision=decision,
            previous_status=request.status,
        )
        self._session.flush()

    def mark_reapproval_required(
        self,
        demand_number: str,
        decision: EnvelopeDecision,
    ) -> None:
        request = self._request(demand_number)
        previous_status = request.status
        request.status = "Soumise"
        request.approved_by_external_id = None
        request.approved_by_name = None
        request.approved_at = None
        request.approval_comment = (
            "Modification hors enveloppe approuvée — nouvelle approbation requise"
        )
        self._append_history(
            request,
            action="Réapprobation requise",
            comment=(
                "La proposition candidate dépasse l'autorisation active; "
                "le plan actif conserve la révision approuvée précédente."
            ),
            decision=decision,
            previous_status=previous_status,
        )
        self._session.flush()

    def stamp_direct_approval(
        self,
        demand_number: str,
        decision: EnvelopeDecision,
        *,
        actor_name: str,
    ) -> None:
        """Stamp an approver-authored widening before atomic revision activation."""

        request = self._request(demand_number)
        request.status = "En planification"
        request.approved_by_name = _text(actor_name) or self._actor_name or None
        request.approved_at = utc_now()
        request.approval_comment = (
            "Autorisation élargie directement par un acteur disposant du droit d'approbation"
        )
        self._append_history(
            request,
            action="Autorisation élargie par approbateur",
            comment=(
                "La modification candidate est approuvée directement; "
                "une nouvelle révision immuable sera activée atomiquement."
            ),
            decision=decision,
            previous_status="En planification",
        )
        self._session.flush()


    def record_direct_approval(
        self,
        demand_number: str,
        decision: EnvelopeDecision,
        *,
        actor_name: str,
    ) -> None:
        """Audit an approver-authored widening after 276C closes the quorum."""

        request = self._request(demand_number)
        self._append_history(
            request,
            action="Autorisation élargie par approbateur",
            comment=(
                "La modification candidate a été approuvée via le quorum 276C; "
                "une nouvelle révision immuable a été activée atomiquement."
            ),
            decision=decision,
            previous_status="Soumise",
        )
        # Preserve the actor used by the historical direct-approval audit surface.
        history_actor = _text(actor_name) or self._actor_name
        if history_actor and self._actor_name != history_actor:
            latest = self._session.scalar(
                select(WorkforceRequestHistory)
                .where(
                    WorkforceRequestHistory.workforce_request_id == request.id,
                    WorkforceRequestHistory.action
                    == "Autorisation élargie par approbateur",
                )
                .order_by(WorkforceRequestHistory.occurred_at.desc())
            )
            if latest is not None:
                latest.actor_name = history_actor
        self._session.flush()
