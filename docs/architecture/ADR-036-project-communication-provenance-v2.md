# ADR-036 — Project communication provenance v2

Status: Accepted  
Date: 2026-11-17  
WorkItem: #855 / R30

## Context

#830 introduced request-owned chantier-contact provenance into the immutable `project_v2` communication snapshot. #855 additionally needs the final renderer to know, for each physical human Shift, which canonical asset associations apply and how many standard hours apply to that resource on that date.

The existing aggregate resource projection is insufficient for those facts: multiple Shifts may collapse into one resource row, while applicable assets and standard hours can differ per Shift. The communication snapshot must therefore preserve the facts before aggregation without creating a new Planning, Shift, availability, contact, or asset authority.

## Decision

### 1. Cumulative request provenance capability

`request_provenance_version` remains the capability marker inside the existing `project_v2` envelope.

- v1 = #830 request/contact provenance.
- v2 = v1 plus complete Shift-level asset associations and applicable standard hours.
- `request_provenance_complete` remains a separate completeness assertion.

No new communication envelope version is introduced.

### 2. Shift-level immutable facts

`ProjectCommunicationRequestAssignment` remains the canonical one-record-per-physical-Shift provenance structure and gains:

- `assets: tuple[ProjectCommunicationAsset, ...]`;
- `standard_hours: float | None`.

For current v2 snapshots, assets are explicit (including an empty tuple) and standard hours are present, finite, and nonnegative. `None` is reserved for historical snapshots that never captured the fact.

Shift and requirement IDs remain traceability identifiers. They are not newly promoted into semantic fingerprint inputs.

### 3. Asset authority and employee-assignment semantics

ADR-026 remains authoritative. `AssetAllocation` is the only physical reservation authority and `asset_shift_association(...)` remains the canonical computed association.

The snapshot retains `association_kind`. Employee-assigned semantics include only associations that establish an operator for the Shift (`OWNED_SHIFT`, `INHERITED_REQUEST`, `INHERITED_RESOURCE_PERIOD`, `INHERITED_PROJECT_DIRECT`, `INHERITED_SEGMENT`). `RELATED_REQUEST` remains a request-context reservation and must not be presented as an asset assigned to the employee. `SEGMENT_CONTEXT` likewise does not establish employee assignment.

No Shift-to-asset persistence is added and allocation behavior is unchanged.

### 4. Standard-hours authority

ADR-021 remains authoritative. The communication SQL read model batch-loads active `ResourceAvailabilityRule` rows and class associations, then evaluates each resource/date through `availability_state_for_day()` using the resource's canonical class.

The resulting `AvailabilityDayState.hours` is snapshotted. No fixed eight-hour rule, request default-hours substitute, or parallel availability algorithm is introduced.

### 5. Historical compatibility

Historical snapshots are never enriched from current live tables.

- no-version snapshots retain P0 semantics;
- v1 snapshots retain the exact P1 request/contact capability;
- v2 snapshots require complete Shift assets and standard hours;
- unknown, malformed, incomplete, or downgraded capability combinations fail closed.

Serialization of no-version and v1 snapshots preserves their historical shape; v2 fields are not injected into v1 JSON.

### 6. Cross-version comparison

- v2 → v2 compares complete v2 semantics normally.
- no-version/v1 communicated baseline → current v2 conservatively marks every continuing project modified once; additions and removals remain normal.
- removals use only the historical project facts actually persisted.
- v2 → v1 is a capability downgrade and fails closed.

This avoids claiming equality for facts older snapshots could not record.

### 7. Fingerprint and staleness

P0 and P1 fingerprint algorithms remain unchanged.

P2/v2 adds an explicit semantic extension that captures the deterministic mapping between date/work/request, resource, Shift hours, applicable standard hours, and Shift-specific asset associations. Physical Shift/requirement IDs are excluded from the new semantic extension.

Consequently a change to chantier contact, ERP manager phone, applicable standard hours, or Shift asset mapping changes the current fingerprint. Swapping two assets between Shifts changes P2 even when the aggregate asset union is unchanged.

Prepared bodies remain immutable. Old prepared/approved P0/P1 batches become incompatible/stale once the current projection is v2 and must be cancelled/re-prepared through the existing workflow; they are never upgraded in place.

### 8. Future HTML/text boundary

#855 later phases may add a durable HTML representation alongside plain text, but both must derive from one semantic renderer and remain one communication authority. Phase 2 does not add HTML fields, migrations, transport changes, or manual-review redesign.

## Rollback implications

The database schema is unchanged by this capability. A rollback to code that cannot understand provenance v2 must not preview, prepare, approve, deliver, regenerate, or rewrite project communication snapshots/fingerprints created under v2. Restore a v2-capable binary before communication operations resume.

## Consequences

The communication projection becomes structurally richer but Planning, Shift mutation, asset allocation, availability, chantier-contact, manager-phone, recipient, and authorization authorities remain unchanged.

A final Architect re-review of the exact implementation PR head is required before merge.

## References

- #830 — request-owned chantier contact
- #831 — ERP project-manager telephone
- #855 — R30 compact/mobile project communication
- ADR-021 — class-scoped holiday/availability authority
- ADR-026 — asset assignment authority and computed Shift propagation
