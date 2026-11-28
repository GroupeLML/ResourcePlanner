"""Canonical superintendent population facts (ADR-039, Phase 1A)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SuperintendentEmployee:
    resource_id: str
    display_name: str
    email: str | None


@dataclass(frozen=True)
class SuperintendentPopulation:
    competency_id: str | None
    employees: tuple[SuperintendentEmployee, ...] = ()
    diagnostic: str | None = None

    @property
    def complete(self) -> bool:
        """A valid empty population is complete; a failed binding is not."""
        return self.diagnostic is None
