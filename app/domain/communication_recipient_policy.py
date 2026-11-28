"""Immutable global recipient-policy facts for ADR-039.

This is the population component of the future v5 context, not a complete v5
projection. Message-specific identity/delivery authority belongs outside this
component; an unusable employee address is evidence, not a population failure.
"""

from __future__ import annotations

from dataclasses import dataclass

from .superintendent_population import SuperintendentEmployee, SuperintendentPopulation
from .superintendent_recipient_resolution import (
    AddressFailure,
    AddressResolution,
    EmployeeAddressDiagnostic,
    SuperintendentAddress,
    resolve_address_v1,
    resolve_superintendent_addresses_v1,
)


POLICY_ID = "SUPERINTENDENT_CC"
POLICY_VERSION = 1
ADDRESS_AUTHORITY = "RESOURCE_EMAIL"
_BINDING_MISSING = "SUPERINTENDENT_BINDING_MISSING"
_BINDING_INVALID = "SUPERINTENDENT_BINDING_INVALID"
_BINDING_INACTIVE = "SUPERINTENDENT_BINDING_INACTIVE"


def _nonblank_id(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _require_keys(value: object, keys: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("Malformed recipient-policy population payload")
    return value


@dataclass(frozen=True, slots=True)
class RecipientPolicyPopulation:
    competency_id: str | None
    population_complete: bool
    binding_diagnostic: str | None
    employees: tuple[SuperintendentAddress, ...]
    policy_id: str = POLICY_ID
    policy_version: int = POLICY_VERSION
    address_authority: str = ADDRESS_AUTHORITY

    def __post_init__(self) -> None:
        if (
            self.policy_id != POLICY_ID
            or type(self.policy_version) is not int
            or self.policy_version != POLICY_VERSION
            or self.address_authority != ADDRESS_AUTHORITY
            or type(self.population_complete) is not bool
            or not isinstance(self.employees, tuple)
        ):
            raise ValueError("Invalid recipient-policy population capability")
        if self.population_complete:
            if self.binding_diagnostic is not None or not _nonblank_id(self.competency_id):
                raise ValueError("Complete population requires a verified binding")
        else:
            if self.employees:
                raise ValueError("Failed binding cannot claim eligible employees")
            if self.binding_diagnostic == _BINDING_MISSING:
                if self.competency_id is not None:
                    raise ValueError("Missing binding cannot claim a competency ID")
            elif self.binding_diagnostic in (_BINDING_INVALID, _BINDING_INACTIVE):
                if not isinstance(self.competency_id, str) or not self.competency_id.strip():
                    raise ValueError("Failed configured binding must retain its ID")
                if self.binding_diagnostic == _BINDING_INACTIVE and not _nonblank_id(self.competency_id):
                    raise ValueError("Inactive binding must retain its exact ID")
            else:
                raise ValueError("Incomplete population requires a known diagnostic")
        resource_ids = []
        for row in self.employees:
            if not isinstance(row, SuperintendentAddress) or not isinstance(row.employee, SuperintendentEmployee):
                raise ValueError("Missing canonical employee facts")
            employee = row.employee
            if (
                not _nonblank_id(employee.resource_id)
                or not isinstance(employee.display_name, str)
                or (employee.email is not None and not isinstance(employee.email, str))
                or not isinstance(row.resolution, AddressResolution)
                or (row.resolution.failure is not None and not isinstance(row.resolution.failure, AddressFailure))
                or row.resolution != resolve_address_v1(employee.email)
            ):
                raise ValueError("Contradictory employee address-resolution facts")
            resource_ids.append(employee.resource_id)
        if resource_ids != sorted(set(resource_ids)):
            raise ValueError("Population identities must be unique and canonically ordered")

    @classmethod
    def capture(cls, population: SuperintendentPopulation) -> RecipientPolicyPopulation:
        """Capture the complete Phase 1A read exactly once, with Phase 1B resolution."""
        if not isinstance(population, SuperintendentPopulation) or not isinstance(population.employees, tuple):
            raise ValueError("Expected immutable canonical superintendent population")
        for employee in population.employees:
            if (
                not isinstance(employee, SuperintendentEmployee)
                or not _nonblank_id(employee.resource_id)
                or not isinstance(employee.display_name, str)
                or (employee.email is not None and not isinstance(employee.email, str))
            ):
                raise ValueError("Invalid canonical employee facts")
        return cls(
            population.competency_id,
            population.complete,
            population.diagnostic,
            resolve_superintendent_addresses_v1(population),
        )

    @property
    def address_diagnostics(self) -> tuple[EmployeeAddressDiagnostic, ...]:
        return tuple(row.diagnostic for row in self.employees if row.diagnostic is not None)

    def to_population(self) -> SuperintendentPopulation:
        """Reconstruct the captured facts, never consult current employee records."""
        return SuperintendentPopulation(
            self.competency_id,
            tuple(row.employee for row in self.employees),
            self.binding_diagnostic,
        )

    def to_payload(self) -> dict:
        """Return an explicit canonical JSON-compatible population representation."""
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "address_authority": self.address_authority,
            "competency_id": self.competency_id,
            "population_complete": self.population_complete,
            "binding_diagnostic": self.binding_diagnostic,
            "employees": [
                {
                    "resource_id": row.employee.resource_id,
                    "display_name": row.employee.display_name,
                    "resource_email": row.employee.email,
                    "resolved_address": row.resolution.address,
                    "address_failure": row.resolution.failure.value if row.resolution.failure is not None else None,
                }
                for row in self.employees
            ],
        }

    @classmethod
    def from_payload(cls, payload: object) -> RecipientPolicyPopulation:
        """Read frozen facts; reject missing, unknown and contradictory claims."""
        data = _require_keys(payload, {
            "policy_id", "policy_version", "address_authority", "competency_id",
            "population_complete", "binding_diagnostic", "employees",
        })
        if not isinstance(data["employees"], list):
            raise ValueError("Missing explicit recipient-policy population")
        employees = []
        for value in data["employees"]:
            row = _require_keys(value, {
                "resource_id", "display_name", "resource_email", "resolved_address", "address_failure",
            })
            failure = row["address_failure"]
            if failure is not None and not isinstance(failure, str):
                raise ValueError("Invalid address failure reason")
            employees.append(SuperintendentAddress(
                SuperintendentEmployee(row["resource_id"], row["display_name"], row["resource_email"]),
                AddressResolution(row["resolved_address"], AddressFailure(failure) if failure is not None else None),
            ))
        return cls(
            data["competency_id"], data["population_complete"], data["binding_diagnostic"],
            tuple(employees), data["policy_id"], data["policy_version"], data["address_authority"],
        )
