# ADR-031 — Quick Shift deletion preserves intent with requirement-scoped date exclusions

Status: Accepted  
Date: 2026-10-20

## Context

Issue #755 changes deletion semantics only for canonical `QUICK_SHIFT` requirements.
Deleting one Shift means reducing the Quick Shift need itself by the exact hours carried
by that Shift. The deleted date must remain empty immediately and across later Planning
rebuilds, while surviving automatic shifts must remain automatic under ADR-001.

The normal Planning rebuild protects locked shifts but replaces unlocked automatic
output. Therefore subtracting `planned_hours` alone is insufficient: a later rebuild
could allocate work back onto the deleted date.

## Decision

### Persistent exclusion ownership

Add an additive relation keyed by:

- `ResourceRequirement.id`;
- one excluded calendar date.

The exclusion is owned by the Quick Shift requirement. It means automatic Planning
generation for that requirement cannot generate work on that date.

It does not modify resource availability and does not affect unrelated requirements.

### Planning projection

The SQL Planning snapshot projects requirement exclusions into the pure
`SegmentInput.excluded_dates` contract.

The pure engine excludes those dates from:

- normal standard-capacity distribution;
- overtime generation;
- shortage/unfilled placeholder generation.

Manual locked allocations remain authoritative and may exist on an excluded date.
The exclusion only constrains automatic generation.

### Deletion transaction

For canonical `ORIGIN_QUICK_SHIFT` only, deletion:

1. acquires the existing Planning mutation/CAS guard;
2. loads the Shift and parent requirement;
3. captures the Shift's exact persisted Decimal hours;
4. subtracts those hours from `planned_hours`;
5. records the deleted date exclusion;
6. performs existing Shift-scoped asset cleanup;
7. deletes the Shift;
8. rebuilds in the same transaction.

Retries reuse the existing idempotency/CAS path. If the Shift no longer exists, the
adapter returns without subtracting again.

### Temporary survivor preservation

The deletion rebuild receives a narrow backend-generated set of surviving Shift IDs for
the affected Quick Shift. Those rows are retained for that one rebuild and their actual
hours consume Planning capacity/budget exactly once.

For calculation only, unlocked preserved survivors are supplied as already-consumed
allocation inputs. Their persisted `locked`, `source`, identity, resource, date, hours,
confirmation, and notes remain unchanged.

This temporary mechanism does not make automatic shifts permanently locked. Later
rebuilds may replace/recalculate them according to ADR-001, except that persisted
excluded dates remain unavailable to automatic generation.

### Final Shift

`ResourceRequirement.planned_hours > 0` remains invariant.

If deleting the final remaining Quick Shift hours would exhaust the budget and there
are no surviving shifts, the parent requirement uses the existing canonical
`Annulé` lifecycle. The historical positive `planned_hours` value is retained and the
cancelled requirement no longer contributes to active Planning.

Segment-level asset compatibility checks are performed before cancellation. Inconsistent
historical states fail safely rather than introducing zero or negative requirement hours.

## Scope

This decision applies only when:

`ResourceRequirement.origin == QUICK_SHIFT`

It does not change:

- `REQUEST`;
- generic `AD_HOC`;
- approval behavior;
- resource availability;
- ordinary requirement deletion/replanning.

Quick Shift identity is never inferred from the absence of a WorkforceRequest.

## Consequences

- Deleted Quick Shift dates remain excluded across unrelated later rebuilds.
- Surviving automatic shifts keep their automatic semantics.
- Manual placement on an excluded date remains possible under existing authorization,
  budget and capacity rules.
- One additive migration is required.
- No frontend business calculation is introduced.

## References

- #755
- #95
- #576
- #659A
- #331
- ADR-001
