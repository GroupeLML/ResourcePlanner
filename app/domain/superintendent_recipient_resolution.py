"""ADR-039 recipient resolution, independent of live message composition."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
import unicodedata

from .superintendent_population import SuperintendentEmployee, SuperintendentPopulation


class AddressFailure(str, Enum):
    MISSING = "MISSING"
    INVALID = "INVALID"
    AMBIGUOUS = "AMBIGUOUS"
    HEADER_INJECTION = "HEADER_INJECTION"


@dataclass(frozen=True)
class AddressResolution:
    address: str | None
    failure: AddressFailure | None = None


# Bare ASCII mailbox syntax supported by the existing delivery transports.
# No display-name wrappers, comments, quoted local parts or SMTPUTF8 inference.
_ATOM = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
_LOCAL = re.compile(rf"{_ATOM}(?:\.{_ATOM})*")
_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def resolve_address_v1(raw: str | None) -> AddressResolution:
    """Validate a single bare mailbox; never parse or select from a list."""
    if raw is None:
        return AddressResolution(None, AddressFailure.MISSING)
    # Inspect the original input before trimming, including trailing newlines.
    if any(unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"} for char in raw):
        return AddressResolution(None, AddressFailure.HEADER_INJECTION)
    address = raw.strip()
    if not address:
        return AddressResolution(None, AddressFailure.MISSING)
    if any(char in address for char in ",;") or address.count("@") > 1:
        return AddressResolution(None, AddressFailure.AMBIGUOUS)
    local, separator, domain = address.partition("@")
    if (
        not separator
        or len(address) > 254
        or len(local) > 64
        or not _LOCAL.fullmatch(local)
        or "." not in domain
        or not all(_LABEL.fullmatch(label) for label in domain.split("."))
    ):
        return AddressResolution(None, AddressFailure.INVALID)
    return AddressResolution(address)


@dataclass(frozen=True)
class EmployeeAddressDiagnostic:
    resource_id: str
    display_name: str
    reason: AddressFailure

    @property
    def message(self) -> str:
        reason = {
            AddressFailure.MISSING: "adresse courriel absente",
            AddressFailure.INVALID: "adresse courriel invalide",
            AddressFailure.AMBIGUOUS: "adresse courriel ambigu\u00eb",
            AddressFailure.HEADER_INJECTION: "adresse courriel contenant des caract\u00e8res de contr\u00f4le interdits",
        }[self.reason]
        return (
            f"Surintendant \u00ab {self.display_name} \u00bb (ressource {self.resource_id}) : {reason}. "
            "Renseignez ou corrigez le courriel dans la fiche employ\u00e9 existante, "
            "enregistrez, puis r\u00e9g\u00e9n\u00e9rez la pr\u00e9visualisation."
        )


@dataclass(frozen=True)
class SuperintendentAddress:
    employee: SuperintendentEmployee
    resolution: AddressResolution

    @property
    def diagnostic(self) -> EmployeeAddressDiagnostic | None:
        if self.resolution.failure is None:
            return None
        return EmployeeAddressDiagnostic(
            self.employee.resource_id, self.employee.display_name, self.resolution.failure
        )


def resolve_superintendent_addresses_v1(
    population: SuperintendentPopulation,
) -> tuple[SuperintendentAddress, ...]:
    """Retain every eligible employee, including unusable Resource.email values."""
    return tuple(
        SuperintendentAddress(employee, resolve_address_v1(employee.email))
        for employee in sorted(population.employees, key=lambda row: row.resource_id)
    )


# Historical policy v1 implementations must remain stable; future policies need new entry points.
resolve_address = resolve_address_v1
resolve_superintendent_addresses = resolve_superintendent_addresses_v1
