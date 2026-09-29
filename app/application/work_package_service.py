from __future__ import annotations

from .commands.work_package import WorkPackageCreateCommand, WorkPackageUpdateCommand
from .errors import ApplicationNotFoundError, call_application_port
from .repository_ports import WorkPackageRepositoryPort
from .results import WorkPackageMutationResult
from .commands.common import validate_date_window


class WorkPackageService:
    """Application rules for WorkPackage creation and editing."""

    def __init__(self, repository: WorkPackageRepositoryPort) -> None:
        self._repository = repository

    def create_command(self, command: WorkPackageCreateCommand) -> WorkPackageMutationResult:
        row = call_application_port(
            lambda: self._repository.create(
                {
                    "project_number": command.project_number,
                    "task_catalog_item_id": command.task_catalog_item_id,
                    "code": command.code,
                    "name": command.name,
                    "description": command.description,
                    "start_date": command.start_date,
                    "end_date": command.end_date,
                    "planned_hours": command.planned_hours,
                    "status": command.status,
                }
            ),
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
