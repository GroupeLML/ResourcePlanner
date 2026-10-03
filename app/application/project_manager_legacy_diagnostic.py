from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .project_managers import (
    EffectiveProjectManager,
    ProjectManagerResolutionService,
    PROJECT_MANAGER_RESOLUTION_RESOLVED,
)


LEGACY_ABSENT = "LEGACY_ABSENT"
LEGACY_MATCHES_CANONICAL = "LEGACY_MATCHES_CANONICAL"
LEGACY_DIVERGES_FROM_CANONICAL = "LEGACY_DIVERGES_FROM_CANONICAL"
CANONICAL_UNRESOLVED_WITH_LEGACY_REFERENCE = (
    "CANONICAL_UNRESOLVED_WITH_LEGACY_REFERENCE"
)
LEGACY_REFERENCE_BROKEN = "LEGACY_REFERENCE_BROKEN"

DIAGNOSTIC_LEGACY_PROJECT_MANAGER_CONTACT_INACTIVE = (
    "LEGACY_PROJECT_MANAGER_CONTACT_INACTIVE"
)
DIAGNOSTIC_LEGACY_PROJECT_MANAGER_CONTACT_DIVERGES = (
    "LEGACY_PROJECT_MANAGER_CONTACT_DIVERGES"
)
DIAGNOSTIC_LEGACY_PROJECT_MANAGER_CONTACT_BROKEN = (
    "LEGACY_PROJECT_MANAGER_CONTACT_BROKEN"
)
DIAGNOSTIC_CANONICAL_PROJECT_MANAGER_UNRESOLVED_WITH_LEGACY_REFERENCE = (
    "CANONICAL_PROJECT_MANAGER_UNRESOLVED_WITH_LEGACY_REFERENCE"
)


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


@dataclass(frozen=True, slots=True)
class LegacyProjectManagerProjectRecord:
    project_id: str
    project_number: str
    legacy_project_manager_contact_id: str | None


@dataclass(frozen=True, slots=True)
class LegacyProjectManagerContactRecord:
    business_contact_id: str
    display_name: str
    active: bool


@dataclass(frozen=True, slots=True)
class LegacyProjectManagerDiagnosticRow:
    project_id: str
    project_number: str
    canonical_primary: EffectiveProjectManager | None
    canonical_diagnostics: tuple[str, ...]
    legacy_project_manager_contact_id: str | None
    legacy_contact: LegacyProjectManagerContactRecord | None
    classification: str
    diagnostics: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LegacyProjectManagerDiagnosticSummary:
    projects_scanned: int
    legacy_null: int
    legacy_matching: int
    legacy_divergent: int
    legacy_broken: int
    canonical_unresolved: int
    legacy_inactive: int


@dataclass(frozen=True, slots=True)
class LegacyProjectManagerDiagnosticReport:
    summary: LegacyProjectManagerDiagnosticSummary
    projects: tuple[LegacyProjectManagerDiagnosticRow, ...]

    @property
    def review_required(self) -> bool:
        return bool(
            self.summary.legacy_divergent
            or self.summary.legacy_broken
            or self.summary.canonical_unresolved
            or self.summary.legacy_inactive
        )


class LegacyProjectManagerDiagnosticRepositoryPort(Protocol):
    def list_projects(self) -> tuple[LegacyProjectManagerProjectRecord, ...]: ...

    def list_contacts(
        self,
        business_contact_ids: tuple[str, ...],
    ) -> tuple[LegacyProjectManagerContactRecord, ...]: ...


class LegacyProjectManagerDiagnosticService:
    """Classify the historical project-manager FK without mutating it."""

    def __init__(
        self,
        repository: LegacyProjectManagerDiagnosticRepositoryPort,
        manager_resolution: ProjectManagerResolutionService,
    ) -> None:
        self._repository = repository
        self._manager_resolution = manager_resolution

    def inspect(self) -> LegacyProjectManagerDiagnosticReport:
        projects = self._repository.list_projects()
        project_ids = tuple(project.project_id for project in projects)
        canonical = self._manager_resolution.resolve_projects(list(project_ids))

        legacy_contact_ids = tuple(
            dict.fromkeys(
                contact_id
                for project in projects
                if (
                    contact_id := _optional_text(
                        project.legacy_project_manager_contact_id
                    )
                )
                is not None
            )
        )
        contacts = {
            row.business_contact_id: row
            for row in self._repository.list_contacts(legacy_contact_ids)
        }

        rows: list[LegacyProjectManagerDiagnosticRow] = []
        legacy_null = 0
        legacy_matching = 0
        legacy_divergent = 0
        legacy_broken = 0
        canonical_unresolved = 0
        legacy_inactive = 0

        for project in projects:
            projection = canonical.get(project.project_id)
            primary = projection.primary if projection is not None else None
            canonical_diagnostics = (
                projection.diagnostics if projection is not None else ()
            )
            legacy_id = _optional_text(
                project.legacy_project_manager_contact_id
            )
            legacy_contact = contacts.get(legacy_id) if legacy_id else None
            diagnostics: list[str] = []

            primary_resolved = bool(
                primary is not None
                and primary.resolution_status
                == PROJECT_MANAGER_RESOLUTION_RESOLVED
                and _optional_text(primary.business_contact_id) is not None
            )
            if legacy_id is None:
                classification = LEGACY_ABSENT
                legacy_null += 1
            elif legacy_contact is None:
                classification = LEGACY_REFERENCE_BROKEN
                legacy_broken += 1
                diagnostics.append(
                    DIAGNOSTIC_LEGACY_PROJECT_MANAGER_CONTACT_BROKEN
                )
            elif not primary_resolved:
                classification = CANONICAL_UNRESOLVED_WITH_LEGACY_REFERENCE
                canonical_unresolved += 1
                diagnostics.append(
                    DIAGNOSTIC_CANONICAL_PROJECT_MANAGER_UNRESOLVED_WITH_LEGACY_REFERENCE
                )
            elif primary is not None and primary.business_contact_id == legacy_id:
                classification = LEGACY_MATCHES_CANONICAL
                legacy_matching += 1
            else:
                classification = LEGACY_DIVERGES_FROM_CANONICAL
                legacy_divergent += 1
                diagnostics.append(
                    DIAGNOSTIC_LEGACY_PROJECT_MANAGER_CONTACT_DIVERGES
                )

            if legacy_contact is not None and not legacy_contact.active:
                legacy_inactive += 1
                diagnostics.append(
                    DIAGNOSTIC_LEGACY_PROJECT_MANAGER_CONTACT_INACTIVE
                )

            rows.append(
                LegacyProjectManagerDiagnosticRow(
                    project_id=project.project_id,
                    project_number=project.project_number,
                    canonical_primary=primary,
                    canonical_diagnostics=canonical_diagnostics,
                    legacy_project_manager_contact_id=legacy_id,
                    legacy_contact=legacy_contact,
                    classification=classification,
                    diagnostics=tuple(dict.fromkeys(diagnostics)),
                )
            )

        return LegacyProjectManagerDiagnosticReport(
            summary=LegacyProjectManagerDiagnosticSummary(
                projects_scanned=len(projects),
                legacy_null=legacy_null,
                legacy_matching=legacy_matching,
                legacy_divergent=legacy_divergent,
                legacy_broken=legacy_broken,
                canonical_unresolved=canonical_unresolved,
                legacy_inactive=legacy_inactive,
            ),
            projects=tuple(rows),
        )
