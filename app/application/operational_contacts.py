from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from ..domain.operational_contacts import (
    ContactCandidate,
    ContactResolution,
    resolve_contact_candidates,
    resolve_coordinator,
    resolve_operational_responsible,
)
from .errors import ApplicationNotFoundError, call_application_port


@dataclass(frozen=True, slots=True)
class RequestLineContactContext:
    line_id: str
    demand_number: str | None
    project_id: str
    project_number: str
    task_id: str | None
    task_code: str | None
    task_label: str | None
    proposed_resource_id: str | None
    proposed_resource_name: str | None
    request_override: ContactCandidate
    task_responsible: ContactCandidate
    project_override: ContactCandidate
    project_manager: ContactCandidate
    resource_coordinator: ContactCandidate
    task_coordinator: ContactCandidate
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RequestLineContactResolution:
    line_id: str
    demand_number: str | None
    project_number: str
    task_id: str | None
    task_code: str | None
    task_label: str | None
    proposed_resource_id: str | None
    proposed_resource_name: str | None
    operational_responsible: ContactResolution
    coordinator: ContactResolution
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MaterializedContactContext:
    requirement_id: str
    shift_id: str | None
    request_line_id: str | None
    demand_number: str | None
    project_id: str
    project_number: str
    approved_request_version: int | None
    approved_contact_context_status: str
    task_id: str | None
    task_code: str | None
    task_label: str | None
    resource_id: str | None
    resource_name: str | None
    shift_override: ContactCandidate
    requirement_override: ContactCandidate
    captured_inherited: ContactCandidate
    operational_responsibility_context_provenance: str
    operational_responsibility_context_version: int | None
    resource_coordinator: ContactCandidate
    task_coordinator: ContactCandidate
    task_context_known: bool = True
    task_context_diagnostics: tuple[str, ...] = ()
    resource_context_known: bool = True
    resource_context_diagnostics: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MaterializedContactResolution:
    subject_type: str
    subject_id: str
    requirement_id: str
    shift_id: str | None
    request_line_id: str | None
    demand_number: str | None
    project_number: str
    approved_request_version: int | None
    approved_contact_context_status: str
    task_id: str | None
    task_code: str | None
    task_label: str | None
    resource_id: str | None
    resource_name: str | None
    operational_responsible: ContactResolution
    coordinator: ContactResolution
    diagnostics: tuple[str, ...] = ()


class OperationalContactRepositoryPort(Protocol):
    def get_workforce_request_contact_context(
        self,
        request_id: str,
    ) -> RequestLineContactContext | None: ...

    def get_request_line_contact_context(
        self,
        line_id: str,
    ) -> RequestLineContactContext | None: ...

    def get_request_line_contact_contexts(
        self,
        line_ids: Sequence[str],
    ) -> Sequence[RequestLineContactContext]: ...

    def get_resource_requirement_contact_context(
        self,
        requirement_id: str,
    ) -> MaterializedContactContext | None: ...

    def get_resource_requirement_contact_contexts(
        self,
        requirement_ids: Sequence[str],
    ) -> Sequence[MaterializedContactContext]: ...

    def get_shift_contact_context(
        self,
        shift_id: str,
    ) -> MaterializedContactContext | None: ...

    def get_shift_contact_contexts(
        self,
        shift_ids: Sequence[str],
    ) -> Sequence[MaterializedContactContext]: ...


class OperationalContactService:
    """Resolve current and approved planning contacts through one hierarchy."""

    def __init__(self, repository: OperationalContactRepositoryPort) -> None:
        self._repository = repository

    @staticmethod
    def _resolve_request_line_context(
        context: RequestLineContactContext,
    ) -> RequestLineContactResolution:
        responsible = resolve_operational_responsible(
            request_override=context.request_override,
            task_responsible=context.task_responsible,
            project_override=context.project_override,
            project_manager=context.project_manager,
        )
        coordinator = resolve_coordinator(
            resource_coordinator=context.resource_coordinator,
            task_coordinator=context.task_coordinator,
        )
        return RequestLineContactResolution(
            line_id=context.line_id,
            demand_number=context.demand_number,
            project_number=context.project_number,
            task_id=context.task_id,
            task_code=context.task_code,
            task_label=context.task_label,
            proposed_resource_id=context.proposed_resource_id,
            proposed_resource_name=context.proposed_resource_name,
            operational_responsible=responsible,
            coordinator=coordinator,
            diagnostics=context.diagnostics,
        )

    def resolve_workforce_request(
        self,
        request_id: str,
    ) -> RequestLineContactResolution:
        wanted = str(request_id or "").strip()
        context = call_application_port(
            lambda: self._repository.get_workforce_request_contact_context(wanted),
            code_prefix="operational_contact_read",
            context={"request_id": wanted},
        )
        if context is None:
            raise ApplicationNotFoundError(
                "La demande est introuvable.",
                code="workforce_request_not_found",
                context={"request_id": wanted},
            )
        return self._resolve_request_line_context(context)

    def resolve_request_lines(
        self,
        line_ids: Sequence[str],
    ) -> tuple[RequestLineContactResolution, ...]:
        wanted = tuple(
            dict.fromkeys(
                str(line_id or "").strip()
                for line_id in line_ids
                if str(line_id or "").strip()
            )
        )
        if not wanted:
            return ()

        bulk_loader = getattr(
            self._repository,
            "get_request_line_contact_contexts",
            None,
        )
        if callable(bulk_loader):
            contexts = call_application_port(
                lambda: bulk_loader(wanted),
                code_prefix="operational_contact_read",
                context={"line_ids": wanted},
            )
        else:
            contexts = tuple(
                call_application_port(
                    lambda line_id=line_id: (
                        self._repository.get_request_line_contact_context(line_id)
                    ),
                    code_prefix="operational_contact_read",
                    context={"line_id": line_id},
                )
                for line_id in wanted
            )
        by_line = {
            context.line_id: context
            for context in contexts
            if context is not None
        }
        return tuple(
            self._resolve_request_line_context(by_line[line_id])
            for line_id in wanted
            if line_id in by_line
        )

    def resolve_request_line(self, line_id: str) -> RequestLineContactResolution:
        wanted = str(line_id or "").strip()
        resolved = self.resolve_request_lines((wanted,))
        if not resolved:
            raise ApplicationNotFoundError(
                "La ligne de demande est introuvable.",
                code="request_line_not_found",
                context={"line_id": wanted},
            )
        return resolved[0]

    def resolve_resource_requirements(
        self,
        requirement_ids: Sequence[str],
    ) -> tuple[MaterializedContactResolution, ...]:
        wanted = tuple(
            dict.fromkeys(
                str(requirement_id or "").strip()
                for requirement_id in requirement_ids
                if str(requirement_id or "").strip()
            )
        )
        if not wanted:
            return ()

        bulk_loader = getattr(
            self._repository,
            "get_resource_requirement_contact_contexts",
            None,
        )
        if callable(bulk_loader):
            contexts = call_application_port(
                lambda: tuple(bulk_loader(wanted)),
                code_prefix="operational_contact_read",
                context={"requirement_ids": list(wanted)},
            )
        else:
            contexts = tuple(
                call_application_port(
                    lambda requirement_id=requirement_id: (
                        self._repository.get_resource_requirement_contact_context(
                            requirement_id
                        )
                    ),
                    code_prefix="operational_contact_read",
                    context={"requirement_id": requirement_id},
                )
                for requirement_id in wanted
            )
        by_requirement = {
            context.requirement_id: context
            for context in contexts
            if context is not None
        }
        return tuple(
            self._resolve_materialized(
                by_requirement[requirement_id],
                subject_type="RESOURCE_REQUIREMENT",
                subject_id=requirement_id,
            )
            for requirement_id in wanted
            if requirement_id in by_requirement
        )

    def resolve_resource_requirement(
        self,
        requirement_id: str,
    ) -> MaterializedContactResolution:
        context = call_application_port(
            lambda: self._repository.get_resource_requirement_contact_context(
                requirement_id
            ),
            code_prefix="operational_contact_read",
            context={"requirement_id": requirement_id},
        )
        if context is None:
            raise ApplicationNotFoundError(
                "Le besoin de ressource est introuvable.",
                code="resource_requirement_not_found",
                context={"requirement_id": requirement_id},
            )
        return self._resolve_materialized(
            context,
            subject_type="RESOURCE_REQUIREMENT",
            subject_id=context.requirement_id,
        )

    def resolve_shifts(
        self,
        shift_ids: Sequence[str],
    ) -> tuple[MaterializedContactResolution, ...]:
        wanted = tuple(
            dict.fromkeys(
                str(shift_id or "").strip()
                for shift_id in shift_ids
                if str(shift_id or "").strip()
            )
        )
        if not wanted:
            return ()

        bulk_loader = getattr(
            self._repository,
            "get_shift_contact_contexts",
            None,
        )
        if callable(bulk_loader):
            contexts = call_application_port(
                lambda: tuple(bulk_loader(wanted)),
                code_prefix="operational_contact_read",
                context={"shift_ids": list(wanted)},
            )
        else:
            contexts = tuple(
                call_application_port(
                    lambda shift_id=shift_id: self._repository.get_shift_contact_context(
                        shift_id
                    ),
                    code_prefix="operational_contact_read",
                    context={"shift_id": shift_id},
                )
                for shift_id in wanted
            )
        by_shift = {
            context.shift_id: context
            for context in contexts
            if context is not None and context.shift_id is not None
        }
        return tuple(
            self._resolve_materialized(
                by_shift[shift_id],
                subject_type="SHIFT",
                subject_id=shift_id,
            )
            for shift_id in wanted
            if shift_id in by_shift
        )

    def resolve_shift(self, shift_id: str) -> MaterializedContactResolution:
        context = call_application_port(
            lambda: self._repository.get_shift_contact_context(shift_id),
            code_prefix="operational_contact_read",
            context={"shift_id": shift_id},
        )
        if context is None:
            raise ApplicationNotFoundError(
                "Le quart est introuvable.",
                code="shift_not_found",
                context={"shift_id": shift_id},
            )
        return self._resolve_materialized(
            context,
            subject_type="SHIFT",
            subject_id=context.shift_id or shift_id,
        )

    @staticmethod
    def _resolve_materialized(
        context: MaterializedContactContext,
        *,
        subject_type: str,
        subject_id: str,
    ) -> MaterializedContactResolution:
        responsible = resolve_operational_responsible(
            shift_override=context.shift_override,
            requirement_override=context.requirement_override,
            captured_inherited=context.captured_inherited,
        )

        if context.resource_coordinator.contact_id is not None:
            coordinator = resolve_coordinator(
                resource_coordinator=context.resource_coordinator,
                task_coordinator=context.task_coordinator,
            )
        elif not context.resource_context_known:
            coordinator = resolve_contact_candidates(
                unresolved_diagnostics=context.resource_context_diagnostics
            )
        elif not context.task_context_known:
            coordinator = resolve_contact_candidates(
                unresolved_diagnostics=context.task_context_diagnostics
            )
        else:
            coordinator = resolve_coordinator(
                resource_coordinator=context.resource_coordinator,
                task_coordinator=context.task_coordinator,
            )

        return MaterializedContactResolution(
            subject_type=subject_type,
            subject_id=subject_id,
            requirement_id=context.requirement_id,
            shift_id=context.shift_id,
            request_line_id=context.request_line_id,
            demand_number=context.demand_number,
            project_number=context.project_number,
            approved_request_version=context.approved_request_version,
            approved_contact_context_status=context.approved_contact_context_status,
            task_id=context.task_id,
            task_code=context.task_code,
            task_label=context.task_label,
            resource_id=context.resource_id,
            resource_name=context.resource_name,
            operational_responsible=responsible,
            coordinator=coordinator,
            diagnostics=context.diagnostics,
        )
