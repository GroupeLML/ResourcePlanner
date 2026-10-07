from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Iterable, Mapping

from .active_days import normalize_active_day_target
from .confirmation import normalize_confirmation
from .demand_periods import (
    PERIOD_KIND_ALTERNATIVE,
    PERIOD_KIND_CUMULATIVE,
    PROPOSED_RESOURCE_MODE_SAME_AS_PERIOD,
    VALID_PERIOD_KINDS,
)


DECISION_WITHIN_ENVELOPE = "WITHIN_ENVELOPE"
DECISION_REAPPROVAL_REQUIRED = "REAPPROVAL_REQUIRED"
DECISION_EXPLICIT_EXCEPTION_REQUIRED = "EXPLICIT_EXCEPTION_REQUIRED"
DECISION_APPROVAL_REFERENCE_UNKNOWN = "APPROVAL_REFERENCE_UNKNOWN"
DECISION_INVALID = "INVALID"

ROLE_PROJECT_MANAGER = "PROJECT_MANAGER"
ROLE_COORDINATOR = "COORDINATOR"
ROLE_ADMIN = "ADMIN"

REASON_UNCHANGED = "UNCHANGED"
REASON_OPERATIONAL_ONLY = "OPERATIONAL_ONLY"
REASON_DELEGATED_TOLERANCE = "DELEGATED_TOLERANCE"
REASON_SCOPE_CHANGED = "SCOPE_CHANGED"
REASON_WINDOW_CHANGED = "WINDOW_CHANGED"
REASON_TOPOLOGY_CHANGED = "TOPOLOGY_CHANGED"
REASON_ENTRY_ADDED = "ENTRY_ADDED"
REASON_ENTRY_REMOVED = "ENTRY_REMOVED"
REASON_BUDGET_INCREASE = "BUDGET_INCREASE"
REASON_BUDGET_REDUCTION = "BUDGET_REDUCTION"
REASON_APPROVAL_REFERENCE_UNKNOWN = "APPROVAL_REFERENCE_UNKNOWN"
REASON_INVALID = "INVALID"

CHANGE_CONFIRMATION = "CONFIRMATION_CHANGED"
CHANGE_ALTERNATIVE_SELECTION = "ALTERNATIVE_SELECTION_CHANGED"
CHANGE_PROPOSED_RESOURCE = "PROPOSED_RESOURCE_CHANGED"
CHANGE_DISTRIBUTION_PREFERENCE = "DISTRIBUTION_PREFERENCE_CHANGED"

_HUNDREDTH = Decimal("0.01")
_PM_TOLERANCE_FACTOR = Decimal("1.20")


def _text(value: object) -> str:
    return str(value or "").strip()


def _hours(value: object, *, field: str) -> Decimal:
    try:
        result = Decimal(str(value)).quantize(_HUNDREDTH)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} doit être un nombre valide.") from exc
    if result <= 0:
        raise ValueError(f"{field} doit être supérieur à zéro.")
    return result


def _optional_text(value: object) -> str | None:
    normalized = _text(value)
    return normalized or None


@dataclass(frozen=True, slots=True, order=True)
class EnvelopeEntryIdentity:
    line_id: str
    period_key: str | None = None

    @property
    def stable_key(self) -> str:
        kind = "PERIOD" if self.period_key is not None else "LINE"
        return json.dumps(
            [kind, self.line_id, self.period_key],
            ensure_ascii=False,
            separators=(",", ":"),
        )


def envelope_entry_identity_from_stable_key(value: object) -> EnvelopeEntryIdentity:
    """Parse one canonical line/period identity without weakening its scope."""

    raw = _text(value)
    try:
        parts = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Une identité d'entrée approuvée est invalide.") from exc
    if (
        not isinstance(parts, list)
        or len(parts) != 3
        or parts[0] not in {"LINE", "PERIOD"}
        or not _text(parts[1])
    ):
        raise ValueError("Une identité d'entrée approuvée est invalide.")
    period_key = _optional_text(parts[2])
    if parts[0] == "LINE" and period_key is not None:
        raise ValueError("Une identité de ligne approuvée ne peut pas contenir de période.")
    if parts[0] == "PERIOD" and period_key is None:
        raise ValueError("Une identité de période approuvée doit contenir une clé stable.")
    return EnvelopeEntryIdentity(
        line_id=_text(parts[1]),
        period_key=period_key,
    )


@dataclass(frozen=True, slots=True, order=True)
class EnvelopeGroupIdentity:
    line_id: str
    group_key: str

    @property
    def stable_key(self) -> str:
        return json.dumps(
            ["GROUP", self.line_id, self.group_key],
            ensure_ascii=False,
            separators=(",", ":"),
        )


@dataclass(frozen=True, slots=True)
class EnvelopePeriodDefinition:
    period_key: str
    start_date: date
    end_date: date
    hours: float | Decimal | None
    source_period_id: str | None = None
    resource_count: int = 1
    kind: str = PERIOD_KIND_CUMULATIVE
    group_key: str | None = None
    confirmation: str = "Tentative"
    confirmation_mode: str | None = None
    selected: bool = False
    proposed_resource_id: str | None = None
    proposed_resource_mode: str | None = None
    same_as_period_key: str | None = None
    desired_active_days: int | None = None


@dataclass(frozen=True, slots=True)
class EnvelopeLineDefinition:
    line_id: str
    project_id: str
    site_id: str | None = None
    location: str | None = None
    slot_count: int = 1
    line_kind: str = "WORKFORCE"
    required_resource_class: str | None = None
    competency_ids: tuple[str, ...] = ()
    task_ref: str | None = None
    work_package_ref: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    hours: float | Decimal | None = None
    confirmation: str = "Tentative"
    proposed_resource_id: str | None = None
    desired_active_days: int | None = None
    periods: tuple[EnvelopePeriodDefinition, ...] = ()
    asset_type_id: str | None = None
    occupancy_policy: str | None = None
    proposed_asset_id: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovalEnvelopeEntry:
    identity: EnvelopeEntryIdentity
    project_id: str
    site_id: str | None
    location: str | None
    slot_count: int
    line_kind: str
    required_resource_class: str | None
    competency_ids: tuple[str, ...]
    task_ref: str | None
    work_package_ref: str | None
    start_date: date
    end_date: date
    hours: Decimal | None
    kind: str
    group: EnvelopeGroupIdentity | None
    source_period_id: str | None
    confirmation: str
    selected: bool
    proposed_resource_id: str | None
    desired_active_days: int | None
    asset_type_id: str | None = None
    occupancy_policy: str | None = None
    proposed_asset_id: str | None = None
    confirmation_mode: str | None = None
    proposed_resource_mode: str | None = None
    same_as_period_key: str | None = None

    def authorization_payload(self) -> dict[str, object]:
        payload = {
            "identity": self.identity.stable_key,
            "project_id": self.project_id,
            "site_id": self.site_id,
            "location": self.location,
            "slot_count": self.slot_count,
            "line_kind": self.line_kind,
            "required_resource_class": self.required_resource_class,
            "competency_ids": list(self.competency_ids),
            "task_ref": self.task_ref,
            "work_package_ref": self.work_package_ref,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "hours": format(self.hours, "f") if self.hours is not None else None,
            "kind": self.kind,
            "group": self.group.stable_key if self.group else None,
        }
        if self.confirmation_mode is not None:
            payload["confirmation_mode"] = self.confirmation_mode
        if self.proposed_resource_mode is not None:
            payload["proposed_resource_mode"] = self.proposed_resource_mode
            payload["same_as_period_key"] = self.same_as_period_key
        if self.line_kind == "ASSET":
            payload.update(asset_type_id=self.asset_type_id, occupancy_policy=self.occupancy_policy)
        return payload

    def snapshot_payload(self) -> dict[str, object]:
        payload = self.authorization_payload()
        payload.update(
            {
                "source_period_id": self.source_period_id,
                "confirmation": self.confirmation,
                "confirmation_mode": self.confirmation_mode,
                "selected": self.selected,
                "proposed_resource_id": self.proposed_resource_id,
                "proposed_resource_mode": self.proposed_resource_mode,
                "same_as_period_key": self.same_as_period_key,
                "desired_active_days": self.desired_active_days,
            }
        )
        if self.line_kind == "ASSET":
            payload["proposed_asset_id"] = self.proposed_asset_id
        return payload


@dataclass(frozen=True, slots=True)
class ApprovalEnvelope:
    entries: tuple[ApprovalEnvelopeEntry, ...]

    @property
    def authorization_fingerprint(self) -> str:
        payload = [entry.authorization_payload() for entry in self.entries]
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_snapshot_payload(self) -> dict[str, object]:
        return {
            "format_version": (
                3
                if any(
                    entry.confirmation_mode is not None
                    or entry.proposed_resource_mode is not None
                    for entry in self.entries
                )
                else 2 if any(entry.line_kind == "ASSET" for entry in self.entries) else 1
            ),
            "authorization_fingerprint": self.authorization_fingerprint,
            "entries": [entry.snapshot_payload() for entry in self.entries],
        }


def approval_envelope_from_snapshot_payload(
    payload: Mapping[str, object],
) -> ApprovalEnvelope:
    """Rehydrate the canonical immutable envelope captured in an approval revision."""

    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("Le snapshot approuvé ne contient pas une liste d'entrées valide.")

    entries: list[ApprovalEnvelopeEntry] = []
    for raw in raw_entries:
        if not isinstance(raw, Mapping):
            raise ValueError("Une entrée du snapshot approuvé est invalide.")
        try:
            identity_parts = json.loads(_text(raw.get("identity")))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("Une identité d'entrée approuvée est invalide.") from exc
        if (
            not isinstance(identity_parts, list)
            or len(identity_parts) != 3
            or identity_parts[0] not in {"LINE", "PERIOD"}
        ):
            raise ValueError("Une identité d'entrée approuvée est invalide.")
        line_id = _text(identity_parts[1])
        period_key = _optional_text(identity_parts[2])
        if not line_id:
            raise ValueError("Une entrée approuvée doit référencer une ligne stable.")

        group: EnvelopeGroupIdentity | None = None
        group_value = _optional_text(raw.get("group"))
        if group_value:
            try:
                group_parts = json.loads(group_value)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("Une identité de groupe approuvé est invalide.") from exc
            if (
                not isinstance(group_parts, list)
                or len(group_parts) != 3
                or group_parts[0] != "GROUP"
                or _text(group_parts[1]) != line_id
                or not _text(group_parts[2])
            ):
                raise ValueError("Une identité de groupe approuvé est invalide.")
            group = EnvelopeGroupIdentity(
                line_id=line_id,
                group_key=_text(group_parts[2]),
            )

        try:
            start_date = date.fromisoformat(_text(raw.get("start_date")))
            end_date = date.fromisoformat(_text(raw.get("end_date")))
        except ValueError as exc:
            raise ValueError("Une fenêtre approuvée est invalide.") from exc

        competencies_raw = raw.get("competency_ids")
        competencies = (
            tuple(_text(value) for value in competencies_raw if _text(value))
            if isinstance(competencies_raw, list)
            else ()
        )
        entry = ApprovalEnvelopeEntry(
            identity=EnvelopeEntryIdentity(
                line_id=line_id,
                period_key=period_key,
            ),
            project_id=_text(raw.get("project_id")),
            site_id=_optional_text(raw.get("site_id")),
            location=_optional_text(raw.get("location")),
            slot_count=max(int(raw.get("slot_count") or 1), 1),
            line_kind=_text(raw.get("line_kind")).upper() or "WORKFORCE",
            required_resource_class=_optional_text(
                raw.get("required_resource_class")
            ),
            competency_ids=_normalized_competency_ids(competencies),
            task_ref=_optional_text(raw.get("task_ref")),
            work_package_ref=_optional_text(raw.get("work_package_ref")),
            start_date=start_date,
            end_date=end_date,
            hours=(_hours(raw["hours"], field=f"Les heures de {line_id}/{period_key or 'LINE'}")
                   if raw.get("hours") is not None else None),
            kind=_text(raw.get("kind")).upper() or PERIOD_KIND_CUMULATIVE,
            group=group,
            source_period_id=_optional_text(raw.get("source_period_id")),
            confirmation=normalize_confirmation(raw.get("confirmation")),
            confirmation_mode=_optional_text(raw.get("confirmation_mode")),
            selected=bool(raw.get("selected")),
            proposed_resource_id=_optional_text(raw.get("proposed_resource_id")),
            proposed_resource_mode=_optional_text(raw.get("proposed_resource_mode")),
            same_as_period_key=_optional_text(raw.get("same_as_period_key")),
            desired_active_days=(
                int(raw["desired_active_days"])
                if raw.get("desired_active_days") is not None
                else None
            ),
            asset_type_id=_optional_text(raw.get("asset_type_id")),
            occupancy_policy=_optional_text(raw.get("occupancy_policy")),
            proposed_asset_id=_optional_text(raw.get("proposed_asset_id")),
        )
        entries.append(entry)

    entries.sort(key=lambda entry: entry.identity.stable_key)
    envelope = ApprovalEnvelope(entries=tuple(entries))
    validate_approval_envelope(envelope)
    return envelope


@dataclass(frozen=True, slots=True)
class EnvelopeChange:
    code: str
    entry_key: str | None = None
    detail: str | None = None
    reference_hours: Decimal | None = None
    candidate_hours: Decimal | None = None
    delta_hours: Decimal | None = None
    delta_percent: Decimal | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "entry_key": self.entry_key,
            "detail": self.detail,
            "reference_hours": (
                format(self.reference_hours, "f")
                if self.reference_hours is not None
                else None
            ),
            "candidate_hours": (
                format(self.candidate_hours, "f")
                if self.candidate_hours is not None
                else None
            ),
            "delta_hours": (
                format(self.delta_hours, "f")
                if self.delta_hours is not None
                else None
            ),
            "delta_percent": (
                format(self.delta_percent, "f")
                if self.delta_percent is not None
                else None
            ),
        }


@dataclass(frozen=True, slots=True)
class EnvelopeDecision:
    decision: str
    reason: str
    changes: tuple[EnvelopeChange, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "changes": [change.to_dict() for change in self.changes],
        }


def _normalized_competency_ids(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({_text(value) for value in values if _text(value)}))


def _validate_window(
    start_date: date,
    end_date: date,
    *,
    label: str,
    desired_active_days: int | None,
) -> None:
    if end_date < start_date:
        raise ValueError(f"{label} se termine avant sa date de début.")
    normalize_active_day_target(
        desired_active_days,
        start=start_date,
        end=end_date,
        field=f"Les jours actifs de {label}",
    )


def normalize_approval_envelope(
    lines: Iterable[EnvelopeLineDefinition],
) -> ApprovalEnvelope:
    """Normalize request lines into the immutable authorization topology.

    Detailed periods are authoritative when present. Simple line dates/hours are used
    only when a line has no explicit periods, so they never create a second budget.
    """

    entries: list[ApprovalEnvelopeEntry] = []
    seen_lines: set[str] = set()

    for raw_line in lines:
        line_id = _text(raw_line.line_id)
        if not line_id:
            raise ValueError("Chaque ligne doit avoir un identifiant stable.")
        if line_id in seen_lines:
            raise ValueError(f"Identifiant de ligne dupliqué: {line_id}")
        seen_lines.add(line_id)

        project_id = _text(raw_line.project_id)
        if not project_id:
            raise ValueError(f"La ligne {line_id} doit référencer un projet.")
        line_kind = _text(raw_line.line_kind).upper() or "WORKFORCE"
        if line_kind not in {"WORKFORCE", "ASSET"}:
            raise ValueError(f"Type de ligne non supporté: {line_kind}")
        if line_kind == "ASSET" and (not raw_line.asset_type_id or raw_line.occupancy_policy != "EXCLUSIVE_DAY"):
            raise ValueError(f"La ligne {line_id} doit préciser un type et une occupation exclusive quotidienne.")
        competencies = _normalized_competency_ids(raw_line.competency_ids)
        common = {
            "project_id": project_id,
            "site_id": _optional_text(raw_line.site_id),
            "location": _optional_text(raw_line.location),
            "line_kind": line_kind,
            "required_resource_class": _optional_text(
                raw_line.required_resource_class
            ),
            "competency_ids": competencies,
            "task_ref": _optional_text(raw_line.task_ref),
            "work_package_ref": _optional_text(raw_line.work_package_ref),
        }
        if line_kind == "ASSET":
            common.update(asset_type_id=raw_line.asset_type_id, occupancy_policy=raw_line.occupancy_policy, proposed_asset_id=raw_line.proposed_asset_id)

        if raw_line.periods:
            seen_periods: set[str] = set()
            group_counts: dict[str, int] = {}
            selected_groups: dict[str, str] = {}

            for period in raw_line.periods:
                period_key = _text(period.period_key)
                if not period_key:
                    raise ValueError(
                        f"Chaque période de la ligne {line_id} doit avoir une clé stable."
                    )
                if period_key in seen_periods:
                    raise ValueError(
                        f"Clé de période dupliquée pour la ligne {line_id}: {period_key}"
                    )
                seen_periods.add(period_key)

                kind = _text(period.kind).upper()
                if kind not in VALID_PERIOD_KINDS:
                    raise ValueError(
                        f"Type de période non supporté pour {line_id}/{period_key}: "
                        f"{period.kind}"
                    )
                group_key = _optional_text(period.group_key)
                if kind == PERIOD_KIND_ALTERNATIVE:
                    if not group_key:
                        raise ValueError(
                            f"La période alternative {line_id}/{period_key} doit "
                            "avoir une clé de groupe stable."
                        )
                    group_counts[group_key] = group_counts.get(group_key, 0) + 1
                    if period.selected:
                        previous = selected_groups.get(group_key)
                        if previous is not None:
                            raise ValueError(
                                f"Le groupe alternatif {line_id}/{group_key} a "
                                "plus d'une option sélectionnée."
                            )
                        selected_groups[group_key] = period_key
                elif group_key:
                    raise ValueError(
                        f"La période cumulative {line_id}/{period_key} ne peut "
                        "pas appartenir à un groupe alternatif."
                    )

                if int(period.resource_count or 0) < 1:
                    raise ValueError(
                        f"Le nombre de ressources de {line_id}/{period_key} "
                        "doit être au moins 1."
                    )
                _validate_window(
                    period.start_date,
                    period.end_date,
                    label=f"la période {line_id}/{period_key}",
                    desired_active_days=period.desired_active_days,
                )
                confirmation = normalize_confirmation(period.confirmation)
                group = (
                    EnvelopeGroupIdentity(line_id=line_id, group_key=group_key)
                    if group_key
                    else None
                )
                entries.append(
                    ApprovalEnvelopeEntry(
                        identity=EnvelopeEntryIdentity(
                            line_id=line_id,
                            period_key=period_key,
                        ),
                        start_date=period.start_date,
                        end_date=period.end_date,
                        hours=(_hours(period.hours, field=f"Les heures de {line_id}/{period_key}")
                               if period.hours is not None else None),
                        kind=kind,
                        group=group,
                        source_period_id=_optional_text(period.source_period_id),
                        slot_count=int(period.resource_count),
                        confirmation=confirmation,
                        confirmation_mode=_optional_text(period.confirmation_mode),
                        selected=bool(period.selected),
                        proposed_resource_id=_optional_text(
                            period.proposed_resource_id
                        ),
                        proposed_resource_mode=_optional_text(period.proposed_resource_mode),
                        same_as_period_key=_optional_text(period.same_as_period_key),
                        desired_active_days=period.desired_active_days,
                        **common,
                    )
                )

            singleton_groups = sorted(
                group for group, count in group_counts.items() if count < 2
            )
            if singleton_groups:
                raise ValueError(
                    f"Un groupe alternatif de la ligne {line_id} doit contenir "
                    "au moins deux options: "
                    + ", ".join(singleton_groups)
                )
            continue

        if int(raw_line.slot_count or 0) < 1:
            raise ValueError(
                f"Le nombre de ressources de la ligne {line_id} doit être au moins 1."
            )
        if raw_line.start_date is None:
            raise ValueError(
                f"La ligne {line_id} doit avoir une date de début lorsqu'elle "
                "n'a pas de périodes explicites."
            )
        end_date = raw_line.end_date or raw_line.start_date
        _validate_window(
            raw_line.start_date,
            end_date,
            label=f"la ligne {line_id}",
            desired_active_days=raw_line.desired_active_days,
        )
        if raw_line.hours is None and line_kind == "WORKFORCE":
            raise ValueError(
                f"La ligne {line_id} doit avoir des heures lorsqu'elle n'a pas "
                "de périodes explicites."
            )
        entries.append(
            ApprovalEnvelopeEntry(
                identity=EnvelopeEntryIdentity(line_id=line_id),
                start_date=raw_line.start_date,
                end_date=end_date,
                hours=(_hours(raw_line.hours, field=f"Les heures de {line_id}")
                       if raw_line.hours is not None else None),
                kind=PERIOD_KIND_CUMULATIVE,
                group=None,
                source_period_id=None,
                slot_count=int(raw_line.slot_count),
                confirmation=normalize_confirmation(raw_line.confirmation),
                confirmation_mode=None,
                selected=True,
                proposed_resource_id=_optional_text(
                    raw_line.proposed_resource_id
                ),
                proposed_resource_mode=None,
                same_as_period_key=None,
                desired_active_days=raw_line.desired_active_days,
                **common,
            )
        )

    entries.sort(key=lambda entry: entry.identity.stable_key)
    envelope = ApprovalEnvelope(entries=tuple(entries))
    validate_approval_envelope(envelope)
    return envelope


def validate_approval_envelope(envelope: ApprovalEnvelope) -> None:
    identities: set[EnvelopeEntryIdentity] = set()
    groups: dict[EnvelopeGroupIdentity, list[ApprovalEnvelopeEntry]] = {}
    for entry in envelope.entries:
        if entry.identity in identities:
            raise ValueError(
                f"Identité d'enveloppe dupliquée: {entry.identity.stable_key}"
            )
        identities.add(entry.identity)
        _validate_window(
            entry.start_date,
            entry.end_date,
            label=f"l'entrée {entry.identity.stable_key}",
            desired_active_days=entry.desired_active_days,
        )
        if entry.slot_count < 1:
            raise ValueError(
                f"Le nombre de ressources de {entry.identity.stable_key} "
                "doit être au moins 1."
            )
        if entry.line_kind == "ASSET" and (not entry.asset_type_id or entry.occupancy_policy != "EXCLUSIVE_DAY"):
            raise ValueError("Une entrée ASSET doit définir son type et sa politique d'occupation.")
        if entry.hours is None and entry.line_kind == "WORKFORCE":
            raise ValueError("Un budget humain est requis.")
        if entry.hours is not None and entry.hours <= 0:
            raise ValueError(
                f"Le budget de {entry.identity.stable_key} doit être positif."
            )
        if entry.kind not in VALID_PERIOD_KINDS:
            raise ValueError(
                f"Type de période non supporté: {entry.kind}"
            )
        if entry.kind == PERIOD_KIND_ALTERNATIVE:
            if entry.group is None:
                raise ValueError(
                    f"L'entrée alternative {entry.identity.stable_key} doit "
                    "avoir un groupe."
                )
            if entry.group.line_id != entry.identity.line_id:
                raise ValueError(
                    "Le groupe alternatif doit appartenir à la même ligne que "
                    "son entrée."
                )
            groups.setdefault(entry.group, []).append(entry)
        elif entry.group is not None:
            raise ValueError(
                f"L'entrée cumulative {entry.identity.stable_key} ne peut pas "
                "avoir de groupe alternatif."
            )

    entries_by_identity = {entry.identity: entry for entry in envelope.entries}
    same_as_edges: dict[EnvelopeEntryIdentity, EnvelopeEntryIdentity] = {}
    for entry in envelope.entries:
        if entry.proposed_resource_mode != PROPOSED_RESOURCE_MODE_SAME_AS_PERIOD:
            continue
        if entry.line_kind != "WORKFORCE" or entry.kind != PERIOD_KIND_CUMULATIVE:
            raise ValueError("SAME_AS_PERIOD est limité aux périodes humaines cumulatives.")
        if entry.group is not None or entry.slot_count != 1:
            raise ValueError("SAME_AS_PERIOD exige une période cumulative de quantité effective 1.")
        target_key = _text(entry.same_as_period_key)
        if not target_key:
            raise ValueError("SAME_AS_PERIOD exige une période cible.")
        target_identity = EnvelopeEntryIdentity(
            line_id=entry.identity.line_id,
            period_key=target_key,
        )
        target = entries_by_identity.get(target_identity)
        if target is None:
            raise ValueError(
                f"SAME_AS_PERIOD référence une période introuvable dans la même ligne: {target_key}."
            )
        if target.line_kind != "WORKFORCE" or target.kind != PERIOD_KIND_CUMULATIVE:
            raise ValueError("La cible SAME_AS_PERIOD doit être une période humaine cumulative.")
        if target.group is not None or target.slot_count != 1:
            raise ValueError("La cible SAME_AS_PERIOD doit avoir une quantité effective de 1.")
        same_as_edges[entry.identity] = target_identity

    visiting: set[EnvelopeEntryIdentity] = set()
    visited: set[EnvelopeEntryIdentity] = set()

    def visit_same_as(identity: EnvelopeEntryIdentity) -> None:
        if identity in visited:
            return
        if identity in visiting:
            raise ValueError("Les références SAME_AS_PERIOD ne peuvent pas former de cycle.")
        visiting.add(identity)
        target = same_as_edges.get(identity)
        if target is not None:
            visit_same_as(target)
        visiting.remove(identity)
        visited.add(identity)

    for identity in same_as_edges:
        visit_same_as(identity)

    for group, options in groups.items():
        if len(options) < 2:
            raise ValueError(
                f"Le groupe alternatif {group.stable_key} doit contenir au "
                "moins deux options."
            )
        if sum(1 for option in options if option.selected) > 1:
            raise ValueError(
                f"Le groupe alternatif {group.stable_key} ne peut avoir qu'une "
                "sélection active."
            )


def _scope_tuple(entry: ApprovalEnvelopeEntry) -> tuple[object, ...]:
    return (
        entry.project_id,
        entry.site_id,
        entry.location,
        entry.slot_count,
        entry.line_kind,
        entry.required_resource_class,
        entry.competency_ids,
        entry.task_ref,
        entry.work_package_ref,
        entry.asset_type_id,
        entry.occupancy_policy,
    )


def _budget_change(
    approved: ApprovalEnvelopeEntry,
    candidate: ApprovalEnvelopeEntry,
    *,
    actor_role: str | None,
) -> tuple[EnvelopeChange, bool, bool]:
    if candidate.hours == approved.hours:
        return (
            EnvelopeChange(
                code=REASON_UNCHANGED,
                entry_key=approved.identity.stable_key,
            ),
            False,
            False,
        )

    if candidate.hours is None or approved.hours is None:
        return (EnvelopeChange(code=REASON_BUDGET_INCREASE if candidate.hours is not None else REASON_BUDGET_REDUCTION,
                               entry_key=approved.identity.stable_key), True, False)
    delta = candidate.hours - approved.hours
    percent = (delta / approved.hours * Decimal("100")).quantize(_HUNDREDTH)
    if delta < 0:
        return (
            EnvelopeChange(
                code=REASON_BUDGET_REDUCTION,
                entry_key=approved.identity.stable_key,
                reference_hours=approved.hours,
                candidate_hours=candidate.hours,
                delta_hours=delta,
                delta_percent=percent,
            ),
            True,
            False,
        )

    delegated = (
        _text(actor_role).upper() == ROLE_PROJECT_MANAGER
        and candidate.hours <= approved.hours * _PM_TOLERANCE_FACTOR
    )
    return (
        EnvelopeChange(
            code=(
                REASON_DELEGATED_TOLERANCE
                if delegated
                else REASON_BUDGET_INCREASE
            ),
            entry_key=approved.identity.stable_key,
            reference_hours=approved.hours,
            candidate_hours=candidate.hours,
            delta_hours=delta,
            delta_percent=percent,
        ),
        not delegated,
        delegated,
    )


def compare_approval_envelopes(
    approved: ApprovalEnvelope | None,
    candidate: ApprovalEnvelope,
    *,
    actor_role: str | None = None,
    approval_reference_known: bool = True,
) -> EnvelopeDecision:
    """Classify a candidate definition against the last approved authorization.

    Confirmation, alternative selection, proposed resource and active-day target are
    operational/distribution choices. They are reported but do not widen the approved
    authorization. Structural topology, scope, windows and local budgets remain
    authoritative.
    """

    if approved is None or not approval_reference_known:
        return EnvelopeDecision(
            decision=DECISION_APPROVAL_REFERENCE_UNKNOWN,
            reason=REASON_APPROVAL_REFERENCE_UNKNOWN,
        )

    try:
        validate_approval_envelope(approved)
        validate_approval_envelope(candidate)
    except ValueError as exc:
        return EnvelopeDecision(
            decision=DECISION_INVALID,
            reason=REASON_INVALID,
            changes=(EnvelopeChange(code=REASON_INVALID, detail=str(exc)),),
        )

    approved_by_id = {entry.identity: entry for entry in approved.entries}
    candidate_by_id = {entry.identity: entry for entry in candidate.entries}
    changes: list[EnvelopeChange] = []
    requires_reapproval = False
    delegated_tolerance = False
    operational_change = False

    for identity in sorted(
        approved_by_id.keys() - candidate_by_id.keys(),
        key=lambda value: value.stable_key,
    ):
        requires_reapproval = True
        changes.append(
            EnvelopeChange(
                code=REASON_ENTRY_REMOVED,
                entry_key=identity.stable_key,
            )
        )

    for identity in sorted(
        candidate_by_id.keys() - approved_by_id.keys(),
        key=lambda value: value.stable_key,
    ):
        requires_reapproval = True
        changes.append(
            EnvelopeChange(
                code=REASON_ENTRY_ADDED,
                entry_key=identity.stable_key,
            )
        )

    for identity in sorted(
        approved_by_id.keys() & candidate_by_id.keys(),
        key=lambda value: value.stable_key,
    ):
        reference = approved_by_id[identity]
        proposed = candidate_by_id[identity]

        if _scope_tuple(reference) != _scope_tuple(proposed):
            requires_reapproval = True
            changes.append(
                EnvelopeChange(
                    code=REASON_SCOPE_CHANGED,
                    entry_key=identity.stable_key,
                )
            )
        if (
            reference.start_date != proposed.start_date
            or reference.end_date != proposed.end_date
        ):
            requires_reapproval = True
            changes.append(
                EnvelopeChange(
                    code=REASON_WINDOW_CHANGED,
                    entry_key=identity.stable_key,
                    detail=(
                        f"{reference.start_date.isoformat()}.."
                        f"{reference.end_date.isoformat()} -> "
                        f"{proposed.start_date.isoformat()}.."
                        f"{proposed.end_date.isoformat()}"
                    ),
                )
            )
        if reference.kind != proposed.kind or reference.group != proposed.group:
            requires_reapproval = True
            changes.append(
                EnvelopeChange(
                    code=REASON_TOPOLOGY_CHANGED,
                    entry_key=identity.stable_key,
                )
            )
        if (
            reference.confirmation_mode != proposed.confirmation_mode
            or reference.proposed_resource_mode != proposed.proposed_resource_mode
            or reference.same_as_period_key != proposed.same_as_period_key
        ):
            requires_reapproval = True
            changes.append(
                EnvelopeChange(
                    code=REASON_TOPOLOGY_CHANGED,
                    entry_key=identity.stable_key,
                    detail="Le mode d'héritage de période a changé.",
                )
            )

        budget_change, budget_requires_reapproval, delegated = _budget_change(
            reference,
            proposed,
            actor_role=actor_role,
        )
        if budget_change.code != REASON_UNCHANGED:
            changes.append(budget_change)
        requires_reapproval = (
            requires_reapproval or budget_requires_reapproval
        )
        delegated_tolerance = delegated_tolerance or delegated

        if reference.confirmation != proposed.confirmation:
            operational_change = True
            changes.append(
                EnvelopeChange(
                    code=CHANGE_CONFIRMATION,
                    entry_key=identity.stable_key,
                )
            )
        if reference.selected != proposed.selected:
            operational_change = True
            changes.append(
                EnvelopeChange(
                    code=CHANGE_ALTERNATIVE_SELECTION,
                    entry_key=identity.stable_key,
                )
            )
        if reference.proposed_resource_id != proposed.proposed_resource_id:
            operational_change = True
            changes.append(
                EnvelopeChange(
                    code=CHANGE_PROPOSED_RESOURCE,
                    entry_key=identity.stable_key,
                )
            )
        if reference.desired_active_days != proposed.desired_active_days:
            operational_change = True
            changes.append(
                EnvelopeChange(
                    code=CHANGE_DISTRIBUTION_PREFERENCE,
                    entry_key=identity.stable_key,
                )
            )
        if reference.proposed_asset_id != proposed.proposed_asset_id:
            operational_change = True
            changes.append(EnvelopeChange(code=CHANGE_PROPOSED_RESOURCE, entry_key=identity.stable_key))

    if requires_reapproval:
        primary = next(
            (
                change.code
                for change in changes
                if change.code
                in {
                    REASON_SCOPE_CHANGED,
                    REASON_WINDOW_CHANGED,
                    REASON_TOPOLOGY_CHANGED,
                    REASON_ENTRY_ADDED,
                    REASON_ENTRY_REMOVED,
                    REASON_BUDGET_INCREASE,
                    REASON_BUDGET_REDUCTION,
                }
            ),
            REASON_SCOPE_CHANGED,
        )
        return EnvelopeDecision(
            decision=DECISION_REAPPROVAL_REQUIRED,
            reason=primary,
            changes=tuple(changes),
        )

    if delegated_tolerance:
        return EnvelopeDecision(
            decision=DECISION_WITHIN_ENVELOPE,
            reason=REASON_DELEGATED_TOLERANCE,
            changes=tuple(changes),
        )

    if operational_change:
        return EnvelopeDecision(
            decision=DECISION_WITHIN_ENVELOPE,
            reason=REASON_OPERATIONAL_ONLY,
            changes=tuple(changes),
        )

    return EnvelopeDecision(
        decision=DECISION_WITHIN_ENVELOPE,
        reason=REASON_UNCHANGED,
        changes=tuple(changes),
    )
