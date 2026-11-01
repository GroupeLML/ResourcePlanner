from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ...application.errors import ApplicationConflictError
from ...application.read_models import DemandOperationalChoiceReadModel
from ...application.repository_ports import DemandOperationalChoiceRepositoryPort
from ...domain.approval_envelope import EnvelopeEntryIdentity, EnvelopeGroupIdentity
from ...domain.confirmation import normalize_confirmation
from .approval_revision_models import (
    APPROVAL_REFERENCE_CAPTURED,
    RequestApprovalReference,
    RequestApprovalRevision,
)
from .base import utc_now
from .models import WorkforceRequest, WorkforceRequestHistory
from .operational_choice_models import RequestOperationalState


def _text(value: object) -> str:
    return str(value or "").strip()


class SqlRequestOperationalChoiceRepository(DemandOperationalChoiceRepositoryPort):
    """Persist active choices independently from the editable request candidate."""

    def __init__(self, session: Session, *, actor_name: str = "") -> None:
        self._session = session
        self._actor_name = _text(actor_name)

    def _request(self, number: str) -> WorkforceRequest:
        wanted = _text(number)
        request = self._session.scalar(
            select(WorkforceRequest).where(
                (WorkforceRequest.id == wanted)
                | (WorkforceRequest.legacy_demand_number == wanted)
            )
        )
        if request is None:
            raise KeyError(f"Demande {wanted} introuvable")
        return request

    def _active_revision(
        self,
        request_id: str,
    ) -> RequestApprovalRevision | None:
        reference = self._session.get(RequestApprovalReference, request_id)
        if (
            reference is None
            or reference.status != APPROVAL_REFERENCE_CAPTURED
            or not reference.active_revision_id
        ):
            return None
        return self._session.get(
            RequestApprovalRevision,
            reference.active_revision_id,
        )

    @staticmethod
    def _snapshot_entries(
        revision: RequestApprovalRevision,
    ) -> tuple[dict[str, object], ...]:
        payload = json.loads(revision.payload_text)
        rows = payload.get("authorization", {}).get("entries", [])
        if not isinstance(rows, list):
            raise ValueError("Le snapshot approuvé ne contient pas une liste d'entrées valide.")
        return tuple(dict(row) for row in rows)

    @classmethod
    def _defaults(
        cls,
        revision: RequestApprovalRevision,
    ) -> tuple[dict[str, str], dict[str, str]]:
        selections: dict[str, str] = {}
        confirmations: dict[str, str] = {}
        for entry in cls._snapshot_entries(revision):
            identity = _text(entry.get("identity"))
            if not identity:
                continue
            confirmation = normalize_confirmation(
                entry.get("confirmation"),
                default="Confirmée",
            )
            confirmations[identity] = confirmation
            group = _text(entry.get("group"))
            if group and bool(entry.get("selected")):
                selections[group] = identity
        return selections, confirmations

    @staticmethod
    def _decode_mapping(value: str) -> dict[str, str]:
        decoded = json.loads(value or "{}")
        if not isinstance(decoded, dict):
            raise ValueError("L'état opérationnel sérialisé est invalide.")
        return {
            _text(key): _text(item)
            for key, item in decoded.items()
            if _text(key) and _text(item)
        }

    @staticmethod
    def _read_model(
        row: RequestOperationalState,
    ) -> DemandOperationalChoiceReadModel:
        return DemandOperationalChoiceReadModel(
            approval_revision_id=row.approval_revision_id,
            version=max(int(row.version or 1), 1),
            selections=SqlRequestOperationalChoiceRepository._decode_mapping(
                row.selections_text
            ),
            confirmations=SqlRequestOperationalChoiceRepository._decode_mapping(
                row.confirmations_text
            ),
            budget_overrides={
                key: float(value)
                for key, value in SqlRequestOperationalChoiceRepository._decode_mapping(
                    row.budget_overrides_text
                ).items()
            },
        )

    def initialize_for_revision(
        self,
        request_id: str,
        revision: RequestApprovalRevision,
        *,
        actor_name: str | None = None,
    ) -> DemandOperationalChoiceReadModel:
        selections, confirmations = self._defaults(revision)
        current = self._session.get(RequestOperationalState, request_id)
        if current is None:
            current = RequestOperationalState(
                workforce_request_id=request_id,
                approval_revision_id=revision.id,
                version=1,
                selections_text=json.dumps(
                    selections,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                confirmations_text=json.dumps(
                    confirmations,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                budget_overrides_text="{}",
                updated_by_name=_text(actor_name) or self._actor_name or None,
            )
            self._session.add(current)
        else:
            previous_budget_overrides = self._decode_mapping(
                current.budget_overrides_text
            )
            approved_hours_by_entry = {
                _text(entry.get("identity")): Decimal(_text(entry.get("hours")))
                for entry in self._snapshot_entries(revision)
                if _text(entry.get("identity")) and _text(entry.get("hours"))
            }
            preserved_budget_overrides: dict[str, str] = {}
            for entry_key, value in previous_budget_overrides.items():
                approved_hours = approved_hours_by_entry.get(entry_key)
                if approved_hours is None:
                    continue
                operational_hours = Decimal(value).quantize(Decimal("0.01"))
                if operational_hours != approved_hours.quantize(Decimal("0.01")):
                    preserved_budget_overrides[entry_key] = format(
                        operational_hours,
                        "f",
                    )

            current.approval_revision_id = revision.id
            current.version = max(int(current.version or 1), 1) + 1
            current.selections_text = json.dumps(
                selections,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            current.confirmations_text = json.dumps(
                confirmations,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            current.budget_overrides_text = json.dumps(
                preserved_budget_overrides,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            current.updated_by_name = _text(actor_name) or self._actor_name or None
        self._session.flush()
        return self._read_model(current)

    def _ensure_state(
        self,
        request: WorkforceRequest,
    ) -> tuple[RequestApprovalRevision, RequestOperationalState]:
        revision = self._active_revision(request.id)
        if revision is None:
            raise ApplicationConflictError(
                "Aucune révision approuvée active ne permet cette mutation opérationnelle.",
                code="approval_reference_unknown",
                context={"demand_number": _text(request.legacy_demand_number) or request.id},
            )
        state = self._session.get(RequestOperationalState, request.id)
        if state is None or state.approval_revision_id != revision.id:
            self.initialize_for_revision(
                request.id,
                revision,
                actor_name=self._actor_name,
            )
            state = self._session.get(RequestOperationalState, request.id)
        if state is None:
            raise RuntimeError("L'état opérationnel n'a pas pu être initialisé.")
        return revision, state

    def state_for_request_id(
        self,
        request_id: str,
    ) -> DemandOperationalChoiceReadModel | None:
        request = self._session.get(WorkforceRequest, request_id)
        if request is None:
            return None
        revision = self._active_revision(request.id)
        if revision is None:
            return None
        _revision, state = self._ensure_state(request)
        return self._read_model(state)

    def state_for_demand(
        self,
        demand_number: str,
    ) -> DemandOperationalChoiceReadModel | None:
        request = self._request(demand_number)
        revision = self._active_revision(request.id)
        if revision is None:
            return None
        _revision, state = self._ensure_state(request)
        return self._read_model(state)

    @staticmethod
    def _assert_expected_version(
        state: RequestOperationalState,
        expected_version: int | None,
        *,
        demand_number: str,
    ) -> None:
        if expected_version is None:
            return
        current = max(int(state.version or 1), 1)
        if int(expected_version) != current:
            raise ApplicationConflictError(
                "Les choix opérationnels ont été modifiés depuis leur lecture.",
                code="operational_choice_version_conflict",
                context={
                    "demand_number": demand_number,
                    "expected_version": int(expected_version),
                    "current_version": current,
                },
            )

    def _cas_write(
        self,
        state: RequestOperationalState,
        *,
        selections: Mapping[str, str],
        confirmations: Mapping[str, str],
        budget_overrides: Mapping[str, str],
        demand_number: str,
    ) -> RequestOperationalState:
        previous_version = max(int(state.version or 1), 1)
        next_version = previous_version + 1
        result = self._session.execute(
            update(RequestOperationalState)
            .where(
                RequestOperationalState.workforce_request_id
                == state.workforce_request_id,
                RequestOperationalState.version == previous_version,
                RequestOperationalState.approval_revision_id
                == state.approval_revision_id,
            )
            .values(
                version=next_version,
                selections_text=json.dumps(
                    dict(selections),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                confirmations_text=json.dumps(
                    dict(confirmations),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                budget_overrides_text=json.dumps(
                    dict(budget_overrides),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                updated_by_name=self._actor_name or None,
                updated_at=utc_now(),
            )
        )
        if int(result.rowcount or 0) != 1:
            raise ApplicationConflictError(
                "Les choix opérationnels ont changé pendant la mutation.",
                code="operational_choice_version_conflict",
                context={
                    "demand_number": demand_number,
                    "expected_version": previous_version,
                },
            )
        self._session.expire(state)
        refreshed = self._session.get(
            RequestOperationalState,
            state.workforce_request_id,
        )
        if refreshed is None:
            raise RuntimeError("L'état opérationnel est introuvable après mise à jour.")
        return refreshed

    def select_alternative(
        self,
        demand_number: str,
        alternative_group: str,
        period_id: str,
        *,
        request_line_id: str | None = None,
        expected_version: int | None = None,
    ) -> DemandOperationalChoiceReadModel:
        request = self._request(demand_number)
        revision, state = self._ensure_state(request)
        business_number = _text(request.legacy_demand_number) or request.id
        self._assert_expected_version(
            state,
            expected_version,
            demand_number=business_number,
        )

        line_id = _text(request_line_id) or request.id
        group_key = EnvelopeGroupIdentity(
            line_id=line_id,
            group_key=_text(alternative_group),
        ).stable_key
        entry_key = EnvelopeEntryIdentity(
            line_id=line_id,
            period_key=_text(period_id),
        ).stable_key
        entries = {
            _text(row.get("identity")): row
            for row in self._snapshot_entries(revision)
        }
        target = entries.get(entry_key)
        if (
            target is None
            or _text(target.get("kind")).upper() != "ALTERNATIVE"
            or _text(target.get("group")) != group_key
        ):
            raise ValueError(
                "L'alternative demandée ne fait pas partie de la révision approuvée active."
            )

        selections = self._decode_mapping(state.selections_text)
        confirmations = self._decode_mapping(state.confirmations_text)
        budget_overrides = self._decode_mapping(state.budget_overrides_text)
        previous = selections.get(group_key)
        if previous == entry_key:
            return self._read_model(state)
        selections[group_key] = entry_key
        updated = self._cas_write(
            state,
            selections=selections,
            confirmations=confirmations,
            budget_overrides=budget_overrides,
            demand_number=business_number,
        )
        self._session.add(
            WorkforceRequestHistory(
                workforce_request_id=request.id,
                action="Sélection alternative opérationnelle",
                status=request.status,
                comment=(
                    f"Ligne {line_id} · groupe {_text(alternative_group)}: "
                    f"{previous or 'aucune'} -> {entry_key}"
                ),
                actor_name=self._actor_name or None,
                occurred_at=utc_now(),
            )
        )
        self._session.flush()
        return self._read_model(updated)

    def set_confirmation(
        self,
        demand_number: str,
        confirmation: str,
        *,
        request_line_id: str | None = None,
        period_id: str | None = None,
        expected_version: int | None = None,
    ) -> DemandOperationalChoiceReadModel:
        request = self._request(demand_number)
        revision, state = self._ensure_state(request)
        business_number = _text(request.legacy_demand_number) or request.id
        self._assert_expected_version(
            state,
            expected_version,
            demand_number=business_number,
        )
        line_id = _text(request_line_id) or request.id
        entry_key = EnvelopeEntryIdentity(
            line_id=line_id,
            period_key=_text(period_id) or None,
        ).stable_key
        entries = {
            _text(row.get("identity")): row
            for row in self._snapshot_entries(revision)
        }
        if entry_key not in entries:
            raise ValueError(
                "Le besoin demandé ne fait pas partie de la révision approuvée active."
            )
        normalized = normalize_confirmation(confirmation)
        selections = self._decode_mapping(state.selections_text)
        confirmations = self._decode_mapping(state.confirmations_text)
        budget_overrides = self._decode_mapping(state.budget_overrides_text)
        previous = confirmations.get(entry_key)
        if previous == normalized:
            return self._read_model(state)
        confirmations[entry_key] = normalized
        updated = self._cas_write(
            state,
            selections=selections,
            confirmations=confirmations,
            budget_overrides=budget_overrides,
            demand_number=business_number,
        )
        self._session.add(
            WorkforceRequestHistory(
                workforce_request_id=request.id,
                action="Confirmation opérationnelle",
                status=request.status,
                comment=f"{entry_key}: {previous or 'héritée'} -> {normalized}",
                actor_name=self._actor_name or None,
                occurred_at=utc_now(),
            )
        )
        self._session.flush()
        return self._read_model(updated)


    def set_budget_override(
        self,
        demand_number: str,
        entry_key: str,
        hours: float,
        *,
        expected_version: int | None = None,
    ) -> DemandOperationalChoiceReadModel:
        request = self._request(demand_number)
        revision, state = self._ensure_state(request)
        business_number = _text(request.legacy_demand_number) or request.id
        self._assert_expected_version(
            state,
            expected_version,
            demand_number=business_number,
        )
        identity = _text(entry_key)
        entries = {
            _text(row.get("identity")): row
            for row in self._snapshot_entries(revision)
        }
        target = entries.get(identity)
        if target is None:
            raise ValueError(
                "Le besoin demandé ne fait pas partie de la révision approuvée active."
            )

        proposed = Decimal(str(hours)).quantize(Decimal("0.01"))
        if proposed <= 0:
            raise ValueError("Les heures opérationnelles doivent être supérieures à zéro.")
        approved = Decimal(_text(target.get("hours"))).quantize(Decimal("0.01"))

        selections = self._decode_mapping(state.selections_text)
        confirmations = self._decode_mapping(state.confirmations_text)
        budget_overrides = self._decode_mapping(state.budget_overrides_text)
        previous = budget_overrides.get(identity)
        if proposed == approved:
            budget_overrides.pop(identity, None)
        else:
            budget_overrides[identity] = format(proposed, "f")
        current_value = budget_overrides.get(identity)
        if previous == current_value:
            return self._read_model(state)

        updated = self._cas_write(
            state,
            selections=selections,
            confirmations=confirmations,
            budget_overrides=budget_overrides,
            demand_number=business_number,
        )
        self._session.add(
            WorkforceRequestHistory(
                workforce_request_id=request.id,
                action="Budget opérationnel",
                status=request.status,
                comment=(
                    f"{identity}: {previous or format(approved, 'f')} -> "
                    f"{current_value or format(approved, 'f')}"
                ),
                details=json.dumps(
                    {
                        "approval_revision_id": revision.id,
                        "approved_entry_key": identity,
                        "approved_hours": format(approved, "f"),
                        "operational_hours": format(proposed, "f"),
                        "operational_version": updated.version,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                actor_name=self._actor_name or None,
                occurred_at=utc_now(),
            )
        )
        self._session.flush()
        return self._read_model(updated)
