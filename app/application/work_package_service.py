from __future__ import annotations

from .commands.common import UNSET
from .commands.work_package import WorkPackageCreateCommand, WorkPackageUpdateCommand
from .errors import ApplicationNotFoundError, call_application_port
from .repository_ports import WorkPackageRepositoryPort
from .results import WorkPackageMutationResult
from .commands.common import validate_date_window
from .work_package_weekly_load import (
    WEEKLY_LOAD_ORIGIN_AUTO,
    WorkPackageWeeklyLoadProposalResult,
    WorkPackageWeeklyLoadReplaceCommand,
    propose_weekly_loads,
)


class WorkPackageService:
    """Application rules for WorkPackage creation and editing."""

    def __init__(self, repository: WorkPackageRepositoryPort) -> None:
        self._repository = repository

    def create_command(self, command: WorkPackageCreateCommand) -> WorkPackageMutationResult:
        values: dict[str, object] = {
            "project_number": command.project_number,
            "task_catalog_item_id": command.task_catalog_item_id,
            "code": command.code,
            "name": command.name,
            "description": command.description,
            "start_date": command.start_date,
            "end_date": command.end_date,
            "planned_hours": command.planned_hours,
        }
        if command.resource_class_code is not UNSET:
            values["resource_class_code"] = command.resource_class_code
        row = call_application_port(
            lambda: self._repository.create(values),
            code_prefix="work_package_create",
            context={"project_number": command.project_number},
        )
        return WorkPackageMutationResult(
            reference=row.reference,
            action="created",
            version=row.version,
        )

    def update_command(self, command: WorkPackageUpdateCommand) -> WorkPackageMutationResult:
        current = call_application_port(
            lambda: self._repository.get(command.reference),
            code_prefix="work_package_read",
            context={"reference": command.reference},
        )
        if current is None:
            raise ApplicationNotFoundError(
                f"WorkPackage {command.reference} introuvable",
                code="work_package_not_found",
                context={"reference": command.reference},
            )

        changes = command.changes()
        start = changes.get("start_date", current.start_date)
        end = changes.get("end_date", current.end_date)
        validate_date_window(start, end, prefix="work_package")  # type: ignore[arg-type]

        row = call_application_port(
            lambda: self._repository.update(
                command.reference,
                changes,
                expected_version=command.expected_version,
            ),
            code_prefix="work_package_update",
            context={"reference": command.reference},
        )
        return WorkPackageMutationResult(
            reference=row.reference,
            action="updated",
            version=row.version,
        )


    def propose_weekly_loads(self, reference: str) -> WorkPackageWeeklyLoadProposalResult:
        state = call_application_port(
            lambda: self._repository.get_weekly_load_state(reference),
            code_prefix="work_package_weekly_load_read",
            context={"reference": reference},
        )
        if state is None:
            raise ApplicationNotFoundError(
                f"WorkPackage {reference} introuvable",
                code="work_package_not_found",
                context={"reference": reference},
            )
        loads = propose_weekly_loads(state)
        assert state.planned_hours is not None
        return WorkPackageWeeklyLoadProposalResult(
            reference=state.reference,
            version=state.version,
            planned_hours=state.planned_hours,
            origin=WEEKLY_LOAD_ORIGIN_AUTO,
            loads=loads,
        )

    def replace_weekly_loads(
        self,
        command: WorkPackageWeeklyLoadReplaceCommand,
    ) -> WorkPackageMutationResult:
        row = call_application_port(
            lambda: self._repository.replace_weekly_loads(
                command.reference,
                command.loads,
                origin=command.origin,
                expected_version=command.expected_version,
            ),
            code_prefix="work_package_weekly_load_replace",
            context={"reference": command.reference},
        )
        return WorkPackageMutationResult(
            reference=row.reference,
            action="weekly_loads_replaced",
            version=row.version,
        )

    def close(self, reference: str, *, expected_version: int) -> WorkPackageMutationResult:
        row = call_application_port(
            lambda: self._repository.set_terminal_status(
                reference,
                terminal_status="closed",
                expected_version=expected_version,
            ),
            code_prefix="work_package_close",
            context={"reference": reference},
        )
        return WorkPackageMutationResult(
            reference=row.reference,
            action="closed",
            version=row.version,
        )

    def cancel(self, reference: str, *, expected_version: int) -> WorkPackageMutationResult:
        row = call_application_port(
            lambda: self._repository.set_terminal_status(
                reference,
                terminal_status="cancelled",
                expected_version=expected_version,
            ),
            code_prefix="work_package_cancel",
            context={"reference": reference},
        )
        return WorkPackageMutationResult(
            reference=row.reference,
            action="cancelled",
            version=row.version,
        )


