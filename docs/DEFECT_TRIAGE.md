# Suspected defects: prerequisite-first triage

This durable DEV process implements the Product Owner's clarification in #753.
It complements the source-of-truth, scope-discipline and escalation rules in
`AGENTS.md`; it does not change runtime business or authorization contracts.

## Evidence before runtime changes

A report called a bug is initially a **suspected defect**, not proof that the
observed runtime behavior is wrong. Before implementing a fix:

1. Synchronize with current `main` and reproduce the reported behavior.
2. Identify the expected contract from current code/tests, the issue, related
   issues and applicable ADRs. Separate that contract from assumptions in the report.
3. Enumerate relevant prerequisites: configuration and prerequisite records,
   applicable `Horaire standard` and capacity, resource availability and active
   flags, selected date/window, authorized scope, workflow/approval/materialization
   state, synchronization, environment configuration and feature flags.
4. Establish which prerequisites were actually satisfied in the affected case.
   Distinguish production observations, repository evidence, synthetic reproduction
   and unverified hypotheses. A synthetic test does not prove production data.
5. Classify a software defect only if the required conditions are fulfilled and
   behavior still contradicts the established contract. Otherwise report the
   missing prerequisite or unresolved evidence; do not broaden runtime semantics
   merely to make the observed row or category appear.

When repository evidence cannot establish a material prerequisite, ask the
Product Owner targeted questions before changing runtime behavior. For example:
Does this resource have an active standard schedule overlapping the selected
window? Is it in the user's authorized scope? Is the request approved and
materialized? Did the relevant synchronization complete? Is the feature configured?

The Product Owner owns ultimate product classification and priority. Escalate to
the Architect before implementation when the intended contract is ambiguous or a
proposed change affects architecture, security or business semantics. Do not build
speculative alternative implementations while awaiting that decision.

## Planning distinctions that must be preserved

Class membership and active status alone do not establish global Planning
schedulability. The established global resource catalog requires an active,
resource-specific standard-schedule date envelope overlapping the viewed window.
A missing, inactive, expired or future schedule must not be bypassed by substituting
the general active-resource catalog.

Do not turn this prerequisite into a new positive-free-hours filter: vacation,
holiday, weekend and occupied-capacity calculations remain governed by the
existing daily rules. A schedule envelope and daily available hours are distinct.

ADR-027 / #618 explicit personal-scope relationships remain separate and unchanged,
including directly coordinated resources without shifts. Do not silently impose
the global catalog predicate on those scoped projections, expand access from a
class label, change master classes, or broaden recommendations/assignment rules.

## Handoff evidence

Record the expected contract, reproduction, prerequisites verified or missing,
actual changed paths, targeted and neighboring test results, and any remaining
production verification. Never describe syntax checks as API/browser tests or
claim a production root cause from a code-path hypothesis alone.
