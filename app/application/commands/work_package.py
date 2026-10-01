from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..errors import ApplicationValidationError
from .common import UNSET, UnsetType, required_text, validate_date_window


@dataclass(frozen=True, slots=True)
class WorkPackageCreateCommand:
    project_number: str
    task_catalog_item_id: str
    name: str
    code: str | None = None
    description: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    planned_hours: float | None = None
    status: str = "planned"
    resource_class_code: str | None | UnsetType = UNSET

    def __post_init__(self) -> None:
        required_text(
            self.project_number,
            field="work_package_project",
            message="Le projet est requis.",
        )
        required_text(
            self.task_catalog_item_id,
            field="work_package_task_catalog_item",
            message="La tâche ERP du WorkPackage est requise.",
        )
        required_text(
            self.name,
            field="work_package_name",
            message="Le nom du WorkPackage est requis.",
        )
        required_text(
            self.status,
            field="work_package_status",
            message="Le statut du WorkPackage est requis.",
        )
        validate_date_window(self.start_date, self.end_date, prefix="work_package")
        if (
            self.resource_class_code is not UNSET
            and self.resource_class_code is not None
        ):
            required_text(
                self.resource_class_code,
                field="work_package_resource_class",
                message="La classe de ressource du WorkPackage est requise.",
            )
        if self.planned_hours is not None and self.planned_hours < 0:
            raise ApplicationValidationError(
                "Les heures prévues ne peuvent pas être négatives.",
                code="work_package_planned_hours_invalid",
                context={"field": "planned_hours", "value": self.planned_hours},
            )


@dataclass(frozen=True, slots=True)
class WorkPackageUpdateCommand:
    reference: str
    expected_version: int
    project_number: str | UnsetType = UNSET
    task_catalog_item_id: str | None | UnsetType = UNSET
    resource_class_code: str | None | UnsetType = UNSET
    code: str | None | UnsetType = UNSET
    name: str | UnsetType = UNSET
    description: str | None | UnsetType = UNSET
    start_date: date | None | UnsetType = UNSET
    end_date: date | None | UnsetType = UNSET
    planned_hours: float | None | UnsetType = UNSET
    status: str | UnsetType = UNSET

    def __post_init__(self) -> None:
        required_text(
            self.reference,
            field="work_package_reference",
            message="La référence du WorkPackage est requise.",
        )
        if self.expected_version < 1:
            raise ApplicationValidationError(
                "La version attendue du WorkPackage doit être au moins 1.",
                code="work_package_version_invalid",
                context={"expected_version": self.expected_version},
            )
        if self.project_number is not UNSET:
            required_text(
                self.project_number,
                field="work_package_project",
                message="Le projet est requis.",
            )
        if (
            self.resource_class_code is not UNSET
            and self.resource_class_code is not None
        ):
            required_text(
                self.resource_class_code,
                field="work_package_resource_class",
                message="La classe de ressource du WorkPackage est requise.",
            )
        if self.name is not UNSET:
            required_text(
                self.name,
                field="work_package_name",
                message="Le nom du WorkPackage est requis.",
            )
        if self.status is not UNSET:
            required_text(
                self.status,
                field="work_package_status",
                message="Le statut du WorkPackage est requis.",
            )
        if self.planned_hours is not UNSET and self.planned_hours is not None and self.planned_hours < 0:
            raise ApplicationValidationError(
                "Les heures prévues ne peuvent pas être négatives.",
                code="work_package_planned_hours_invalid",
                context={"field": "planned_hours", "value": self.planned_hours},
            )

    def changes(self) -> dict[str, object]:
        values: dict[str, object] = {}
        for field in (
            "project_number",
            "task_catalog_item_id",
            "resource_class_code",
            "code",
            "name",
            "description",
            "start_date",
            "end_date",
            "planned_hours",
            "status",
        ):
            value = getattr(self, field)
            if value is not UNSET:
                values[field] = value
        return values
