from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from .command_ports import AllocationCommandPort
from .commands import QuickShiftCreateCommand
from .errors import (
    ApplicationError,
    ApplicationOperationError,
    ApplicationValidationError,
    call_application_port,
)
from .repository_ports import SegmentRepositoryPort


@dataclass(frozen=True, slots=True)
class QuickShiftResult:
    segment_id: str
    allocation_id: str


class QuickShiftService:
    """Create an ad-hoc segment and its locked shift as one application workflow."""

    ORIGIN_FIELD = "OrigineSegment"
    ORIGIN_QUICK_SHIFT = "QUICK_SHIFT"

    def __init__(
        self,
        segments: SegmentRepositoryPort,
        allocations: AllocationCommandPort,
    ) -> None:
        self._segments = segments
        self._allocations = allocations

    @staticmethod
    def _required(value: object, *, code: str, message: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ApplicationValidationError(message, code=code)
        return normalized

    @staticmethod
    def _positive_hours(value: object) -> float:
        try:
            hours = float(value)
        except (TypeError, ValueError) as exc:
            raise ApplicationValidationError(
                "Les heures du quart doivent être numériques.",
                code="quick_shift_hours_invalid",
                context={"value": value},
            ) from exc
        if hours <= 0:
            raise ApplicationValidationError(
                "Les heures du quart doivent être supérieures à zéro.",
                code="quick_shift_hours_invalid",
                context={"value": hours},
            )
        return round(hours, 2)

    @staticmethod
    def _day(value: object) -> date:
        if not isinstance(value, date):
            raise ApplicationValidationError(
                "La date du quart rapide est requise.",
                code="quick_shift_day_invalid",
                context={"value": value},
            )
        return value

    @staticmethod
    def _created_identifier(value: object, *, entity: str) -> str:
        identifier = str(value or "").strip()
        if not identifier:
            raise ApplicationOperationError(
                f"La création n'a retourné aucun identifiant pour {entity}.",
                code=f"quick_shift_{entity}_id_missing",
            )
        return identifier

    def create_command(self, command: QuickShiftCreateCommand) -> QuickShiftResult:
        project = self._required(
            command.project_number,
            code="quick_shift_project_required",
            message="Un projet est requis pour le quart rapide.",
        )
        tech = self._required(
            command.technician,
            code="quick_shift_resource_required",
            message="Une ressource est requise pour le quart rapide.",
        )
        day = self._day(command.day)
        hours = self._positive_hours(command.hours)
        label = str(command.description or "").strip() or "Quart rapide"

        segment_values = {
            "NoDemande": None,
            "NumeroProjet": project,
            "NomProjet": str(command.project_name or "").strip() or None,
            "Technicien": tech,
            "DateDebut": day,
            "DateFin": day,
            "HeuresPrevues": hours,
            "Statut": "Planifié",
            "Description": label,
            "SourceEffortRow": None,
            "TypePlanification": "Fixe",
            "Priorite": "Normale",
            "Confirmation": command.confirmation,
            self.ORIGIN_FIELD: self.ORIGIN_QUICK_SHIFT,
        }

        segment_id = self._created_identifier(
            call_application_port(
                lambda: self._segments.create(segment_values),
                code_prefix="quick_shift_segment_create",
                context={"project_number": project, "technician": tech},
            ),
            entity="segment",
        )

        try:
            allocation_id = self._created_identifier(
                call_application_port(
                    lambda: self._allocations.create_manual(
                        segment_id,
                        tech,
                        day,
                        hours,
                        bool(command.outside_standard_hours),
                        str(command.note or "").strip(),
                    ),
                    code_prefix="quick_shift_allocation_create",
                    context={"segment_id": segment_id, "technician": tech},
                ),
                entity="allocation",
            )
        except ApplicationError:
            # Rollback is intentionally specialized here because Quick Shift creates
            # two persisted objects as one application workflow. If allocation creation
            # fails, the just-created segment must not remain active by itself.
            try:
                self._segments.update(segment_id, {"Statut": "Annulé"})
            except Exception as rollback_exc:
                raise ApplicationOperationError(
                    f"Le quart rapide n'a pas été créé et le segment {segment_id} "
                    "n'a pas pu être annulé automatiquement.",
                    code="quick_shift_rollback_failed",
                    context={
                        "segment_id": segment_id,
                        "project_number": project,
                        "technician": tech,
                    },
                ) from rollback_exc
            raise

        return QuickShiftResult(segment_id=segment_id, allocation_id=allocation_id)

    def create(
        self,
        *,
        project_number: str,
        project_name: str = "",
        technician: str,
        day_value: Any,
        hours_value: Any,
        hors_horaire: bool = False,
        note: str = "",
        description: str = "",
        confirmation: str = "Confirmée",
    ) -> QuickShiftResult:
        """Compatibility adapter for the current NiceGUI dialog."""

        return self.create_command(
            QuickShiftCreateCommand.from_values(
                project_number=project_number,
                project_name=project_name,
                technician=technician,
                day_value=day_value,
                hours_value=hours_value,
                outside_standard_hours=hors_horaire,
                note=note,
                description=description,
                confirmation=confirmation,
            )
        )
