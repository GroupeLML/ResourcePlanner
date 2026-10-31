from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from .command_ports import AllocationCommandPort, ApprovedDemandSyncPort, PlanningCommandPort
from .commands import SegmentCancelCommand, SegmentCreateCommand, SegmentUpdateCommand
from .errors import (
    ApplicationNotFoundError,
    ApplicationOperationError,
    ApplicationValidationError,
    call_application_port,
)
from .repository_ports import PlanningAuthorizationPort, SegmentRepositoryPort


class SegmentService:
    """Application boundary for operational segment mutations."""

    def __init__(
        self,
        segments: SegmentRepositoryPort,
        planning: PlanningCommandPort,
        authorization: PlanningAuthorizationPort | None = None,
        approved_sync: ApprovedDemandSyncPort | None = None,
        targeting: AllocationCommandPort | None = None,
    ) -> None:
        self._segments = segments
        self._planning = planning
        self._authorization = authorization
        self._approved_sync = approved_sync
        self._targeting = targeting

    @staticmethod
    def _identifier(value: object) -> str:
        identifier = str(value or "").strip()
        if not identifier:
            raise ApplicationValidationError(
                "Le segment est requis.",
                code="segment_id_required",
            )
        return identifier

    @staticmethod
    def _validate_window(start: date | None, end: date | None) -> None:
        if start is None:
            raise ApplicationValidationError(
                "La date de début est requise.",
                code="segment_start_required",
                context={"field": "start_date"},
            )
        if end is not None and end < start:
            raise ApplicationValidationError(
                "La date de fin ne peut pas précéder la date de début.",
                code="segment_date_window_invalid",
                context={"start": start.isoformat(), "end": end.isoformat()},
            )

    def create_command(self, command: SegmentCreateCommand) -> tuple[str, dict[str, Any]]:
        values = command.to_repository_values()
        if self._authorization is not None:
            call_application_port(
                lambda: self._authorization.authorize_segment_create(values),
                code_prefix="segment_authorization_create",
                context={"demand_number": command.demand_number},
            )
        identifier = str(
            call_application_port(
                lambda: self._segments.create(values),
                code_prefix="segment_create",
                context={"demand_number": command.demand_number},
            )
            or ""
        ).strip()
        if not identifier:
            raise ApplicationOperationError(
                "La création du segment n'a retourné aucun identifiant.",
                code="segment_create_id_missing",
                context={"demand_number": command.demand_number},
            )
        summary = call_application_port(
            self._planning.rebuild,
            code_prefix="segment_create_rebuild",
            context={"segment_id": identifier},
        )
        return identifier, dict(summary)

    def update_command(self, command: SegmentUpdateCommand) -> dict[str, Any]:
        identifier = self._identifier(command.segment_id)
        existing = call_application_port(
            lambda: self._segments.get(identifier),
            code_prefix="segment_lookup",
            context={"segment_id": identifier},
        )
        if existing is None:
            raise ApplicationNotFoundError(
                f"Segment {identifier} introuvable",
                code="segment_not_found",
                context={"segment_id": identifier},
            )

        origin = str(existing.origin or "").strip().upper()
        if (
            not str(existing.demand_number or "").strip()
            and origin not in {"QUICK_SHIFT", "AD_HOC"}
        ):
            raise ApplicationValidationError(
                "Un segment sans demande doit provenir d'un besoin ad hoc canonique.",
                code="segment_autonomous_origin_required",
                context={"segment_id": identifier, "origin": existing.origin},
            )

        values = command.to_repository_values()
        pre_mutation_effective_hours_by_segment: dict[str, float] = {}
        if "NoDemande" in values and not str(values["NoDemande"] or "").strip():
            raise ApplicationValidationError(
                "La demande est requise.",
                code="segment_demand_required",
                context={"field": "demand_number"},
            )
        if "HeuresPrevues" in values and (
            values["HeuresPrevues"] is None or float(values["HeuresPrevues"]) <= 0
        ):
            raise ApplicationValidationError(
                "Les heures prévues doivent être supérieures à zéro.",
                code="segment_hours_invalid",
                context={"field": "planned_hours", "value": values["HeuresPrevues"]},
            )
        if "HeuresPrevues" in values:
            proposed_hours = float(values["HeuresPrevues"])
            projection_action = getattr(
                self._segments,
                "planned_hours_overallocation_context",
                None,
            )
            projection = (
                projection_action(identifier, proposed_hours)
                if callable(projection_action)
                else None
            )
            if projection is not None:
                current_effective = projection.get("current_planned_hours")
                if current_effective is not None:
                    pre_mutation_effective_hours_by_segment[identifier] = float(
                        current_effective
                    )
                current_excess = float(projection.get("current_excess_hours", 0.0))
                proposed_excess = float(projection.get("excess_hours", 0.0))
                if (
                    proposed_excess > current_excess + 0.001
                    and not command.allow_locked_overallocation
                ):
                    raise ApplicationValidationError(
                        "Les heures prévues augmenteraient la surallocation existante. Choisis explicitement de conserver la surallocation comme dérogation.",
                        code="segment_overallocation_choice_required",
                        context=dict(projection),
                    )
            else:
                locked_hours = float(existing.locked_hours or 0.0)
                current_excess = max(locked_hours - float(existing.planned_hours), 0.0)
                proposed_excess = max(locked_hours - proposed_hours, 0.0)
                if (
                    proposed_excess > current_excess + 0.001
                    and not command.allow_locked_overallocation
                ):
                    raise ApplicationValidationError(
                        "Les heures prévues seraient inférieures aux quarts manuels verrouillés. Choisis explicitement d'ajuster les heures prévues ou de conserver la surallocation comme dérogation.",
                        code="segment_overallocation_choice_required",
                        context={
                            "segment_id": identifier,
                            "planned_hours": round(proposed_hours, 2),
                            "current_planned_hours": round(float(existing.planned_hours), 2),
                            "locked_hours": round(locked_hours, 2),
                            "operational_base_hours": round(float(existing.planned_hours), 2),
                            "safe_operational_base_hours": round(locked_hours, 2),
                            "current_excess_hours": round(current_excess, 2),
                            "excess_hours": round(proposed_excess, 2),
                        },
                    )
        start = values.get("DateDebut", existing.start_date)
        end = values.get("DateFin", existing.end_date)
        self._validate_window(
            start if isinstance(start, date) else None,
            end if isinstance(end, date) else None,
        )
        target_supplied = "Technicien" in values and origin == "REQUEST"
        target_reference = (
            str(values.get("Technicien") or "").strip()
            if target_supplied
            else ""
        )
        current_target_references = {
            str(existing.automatic_target_resource_id or "").strip(),
            str(existing.automatic_target_resource_name or "").strip(),
            str(existing.resource_name or "").strip(),
        }
        current_target_references.discard("")
        target_change = bool(
            target_supplied
            and target_reference
            and target_reference not in current_target_references
        )
        target_clear = bool(target_supplied and not target_reference)
        if target_clear:
            raise ApplicationValidationError(
                "Le retrait de la cible automatique n'est pas pris en charge par PATCH /segments. Utilise la commande Planning appropriée.",
                code="segment_target_clear_unsupported",
                context={"segment_id": identifier},
            )
        if target_change and self._targeting is None:
            raise ApplicationOperationError(
                "La sélection de cible Planning n'est pas configurée pour cette mutation.",
                code="segment_targeting_unavailable",
                context={"segment_id": identifier},
            )

        if self._authorization is not None:
            call_application_port(
                lambda: self._authorization.authorize_segment_update(
                    identifier,
                    values,
                ),
                code_prefix="segment_authorization_update",
                context={"segment_id": identifier},
            )

        repository_values = dict(values)
        if target_supplied:
            # ADR-004/#781: REQUEST target fields are never projected directly.
            # A real change goes through canonical assignment below; an unchanged
            # value is a no-op so ordinary SegmentEditor saves cannot regenerate
            # ADR-034 returned/reduced work.
            repository_values.pop("Technicien", None)
        call_application_port(
            lambda: self._segments.update(identifier, repository_values),
            code_prefix="segment_update",
            context={"segment_id": identifier},
        )

        target_summary: dict[str, Any] | None = None
        if target_change:
            target_summary = dict(
                call_application_port(
                    lambda: self._targeting.assign_segment(identifier, target_reference),
                    code_prefix="segment_target_update",
                    context={"segment_id": identifier, "technician": target_reference},
                )
            )
        if (
            "HeuresPrevues" in values
            and self._approved_sync is not None
            and existing.origin == "REQUEST"
            and str(existing.demand_number or "").strip()
        ):
            sync_kwargs: dict[str, Any] = {}
            if command.allow_locked_overallocation:
                sync_kwargs["acknowledged_overallocation_segment_ids"] = (identifier,)
            if pre_mutation_effective_hours_by_segment:
                sync_kwargs["pre_mutation_effective_hours_by_segment"] = dict(
                    pre_mutation_effective_hours_by_segment
                )
            call_application_port(
                lambda: self._approved_sync.sync_operational_choices(
                    str(existing.demand_number).strip(),
                    **sync_kwargs,
                ),
                code_prefix="segment_update_effective_need_sync",
                context={"segment_id": identifier},
            )
        if target_summary is not None and "HeuresPrevues" not in values:
            return target_summary
        return dict(
            call_application_port(
                self._planning.rebuild,
                code_prefix="segment_update_rebuild",
                context={"segment_id": identifier},
            )
        )

    def cancel_command(self, command: SegmentCancelCommand) -> dict[str, Any]:
        identifier = self._identifier(command.segment_id)
        existing = call_application_port(
            lambda: self._segments.get(identifier),
            code_prefix="segment_lookup",
            context={"segment_id": identifier},
        )
        if existing is None:
            raise ApplicationNotFoundError(
                f"Segment {identifier} introuvable",
                code="segment_not_found",
                context={"segment_id": identifier},
            )
        call_application_port(
            lambda: self._segments.update(identifier, {"Statut": "Annulé"}),
            code_prefix="segment_cancel",
            context={"segment_id": identifier},
        )
        return dict(
            call_application_port(
                self._planning.rebuild,
                code_prefix="segment_cancel_rebuild",
                context={"segment_id": identifier},
            )
        )

    def create(self, data: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        """Compatibility adapter for the current NiceGUI segment editor."""

        return self.create_command(SegmentCreateCommand.from_mapping(data))

    def update(self, segment_id: str, updates: Mapping[str, Any]) -> dict[str, Any]:
        return self.update_command(SegmentUpdateCommand.from_mapping(segment_id, updates))

    def cancel(self, segment_id: str) -> dict[str, Any]:
        return self.cancel_command(SegmentCancelCommand(segment_id))
