# ADR-037 — Project communication provenance v4 and Shift-level delta semantics

Status: Accepted

Date: 2026-11-19

WorkItem: #889

Decision authority: Product Owner acceptance of the Architect's provenance-v4 decision.

## Relationship to ADR-039

[ADR-039 — Mandatory superintendent CC and recipient-policy provenance v5](ADR-039-mandatory-superintendent-cc-and-recipient-provenance-v5.md) extends this decision forward with v5/P5 recipient-policy provenance, the explicit cross-project superintendent CC exception and project-only new scheduling generation. Historical P0-P4 serialized shapes/fingerprints and this ADR's genuine project/Shift delta and baseline rules remain unchanged.

For v5, changes confined to the global superintendent recipient context do not create an independent delta, and the v4-to-v5 capability transition alone does not require a Shift historical refresh. Existing missing-fact refresh rules continue to apply where older snapshots lack Shift facts. See ADR-039 for employee-email correction, narrowly scoped legacy retirement, cutover and rollback obligations.

## Context

#889 requires an explicit choice between a full project communication and a communication containing only changes since the latest effectively communicated snapshot of the same week. Within an affected project, unchanged Shifts must not be repeated once complete comparison facts are available.

The existing `project_v2` snapshot already contains one `ProjectCommunicationRequestAssignment` per canonical `shift_id`. However, provenance v3 does not retain `confirmation`, `allocation_type`, or `outside_schedule` on that assignment. They are collapsed into resource-level unions or a boolean OR. Several Shifts may share a resource, task description, and date, so those aggregates cannot prove a historical value for a particular Shift.

Task grouping also merges task IDs/codes by description and loses their per-Shift correspondence. In addition, the historical fingerprint algorithms deliberately exclude physical Shift identity. They can therefore treat removal and addition of different Shift IDs as equal when the remaining communication facts are equal.

The accepted solution must preserve immutable communicated history, the existing approval and delivery lifecycle, canonical recipient authority, and the Planning boundary. It must not reconstruct missing historical facts from live data or add another baseline store.

## Decision

### 1. Preserve the envelope, baseline, and authority

- Keep `communication_batches.model_version = project_v2` and the existing `project_snapshot_json` payload.
- Keep a full immutable snapshot in every prepared full or delta batch.
- Select the latest `COMMUNICATED` project snapshot for the same week through the existing selector, including when that batch delivered a delta.
- Keep explicit approval, Graph draft creation, SMTP delivery, manual `Confirmer communiqué`, audit, cancellation, and retry semantics.
- A Graph/M365 draft is not proof of sending. PREPARED, APPROVED, and draft-only batches are not communicated baselines.
- Complete SMTP success establishes COMMUNICATED through the existing workflow. Partial or failed delivery does not establish a complete baseline, and retry must continue to skip messages already SENT.
- Do not introduce a per-recipient or per-project partial baseline, a new baseline table, or another durable delta store.
- Python/backend remains the snapshot and delta authority. No Planning/Shift mutation, browser delta authority, direct browser transport call, or automatic send is introduced.

### 2. Introduce cumulative request provenance v4

`request_provenance_version` remains the capability marker inside `project_v2`; `request_provenance_complete` remains a separate assertion of complete assignment coverage.

| Version | Capability |
|---|---|
| v1 | Original request/contact provenance. |
| v2 | v1 plus explicit per-Shift asset associations and applicable standard hours. |
| v3 | v2 plus explicit request-owned project-manager phone selection and its stored number. |
| v4 | v3 plus the six Shift facts below and identity-sensitive Shift delta semantics. |

Every current v4 `ProjectCommunicationRequestAssignment` retains all existing v1-v3 facts and additionally persists:

| Field | Required v4 fact |
|---|---|
| `confirmation` | Explicit canonical effective confirmation from the authoritative projection input. |
| `allocation_type` | Explicit resolved allocation type from that input. |
| `outside_schedule` | Explicit genuine boolean, including `False`. |
| `task_id` | Explicit canonical task identity, nullable when there is no canonical task. |
| `task_code` | Explicit nullable task code. |
| `task_description` | Explicit Shift-level communication description. |

Project and date remain supplied by the containing immutable project/day structure; no additional project/date fields are required on the assignment.

Capture these facts from each authoritative `ProjectCommunicationAssignment` before resource/task aggregation. Do not recover them from resource aggregates, task ID/code unions, or display order. Retained aggregates remain presentation projections and must be consistent with the per-Shift facts.

No new Planning, task, availability, asset, responsibility, contact, or recipient authority is established.

### 3. Preserve historical contracts and validate capabilities

Valid v1/v2/v3 snapshots retain their historical deserialization behavior, canonical serialized shapes, and historical fingerprint algorithms. Historical no-version/P0 snapshots remain readable under their established contract. Persisted historical JSON and stored fingerprints are never rewritten.

Do not inject v4 fields into older serialized versions. Missing historical facts mean unknown, never false, a default, a current live value, or a value inferred from resource aggregates. In particular, v1 lacks the v2 Shift asset/standard-hours capability, and v1/v2 lack v3 request-owned phone selection; those limitations must not be hidden during comparison.

Historical snapshots must never consult current live Planning, requests, assets, availability, resources, or other mutable business tables for enrichment.

Builder, direct-projection validation, raw snapshot deserialization, serialization, fingerprinting, and comparison must enforce the same capability contract:

- Accept only supported integer capability values, excluding booleans masquerading as integers; require `request_provenance_complete is True` for versioned complete projections.
- Require all six v4 keys in v4 assignments, including explicitly nullable task keys. Null task identity/code is a captured absence in v4, not permission to omit the keys.
- Require canonical valid confirmation, nonblank resolved allocation type and task description, and genuine boolean `outside_schedule`. Do not coerce malformed JSON or fill absent facts while deserializing.
- Preserve the existing finite/valid hours, explicit assets, applicable standard hours, canonical request provenance, assignment coverage, and aggregate consistency rules.
- Require unique nonblank canonical Shift IDs across a snapshot and exactly one assignment per participating physical Shift. Preserve the correlation with resource and operational-responsibility facts.
- Apply all cumulative v3 phone safeguards to v4: genuine boolean opt-in, request-owned provenance, nonblank phone when enabled, and rejection of nonblank legacy `project_manager_erp_phone` facts. A legitimate `False` with a stored unused phone remains valid and unpublished.
- Reject v4-only JSON keys presented as v1/v2/v3, even if their values are null/default. Historical construction/serialization may represent facts that were never captured as absent, but must not silently discard populated v4 assignment facts from a malformed historical projection.
- Reject unknown versions, incomplete or falsely complete versioned projections, and capability downgrades. A compatibility refresh is not a substitute for validation.

### 4. Match by canonical Shift identity within project scope

Use canonical `shift_id` for matching within each project. Names, row positions, dates, hours, text similarity, and requirement identity must not establish an alternative Shift identity.

| Comparison evidence | Shift delta result |
|---|---|
| Shift exists only before | `Retiré`. |
| Shift exists only after | `Ajouté`. |
| Same Shift ID, resource changed | `Remplacé`. |
| Same Shift ID, same resource, date changed | `Déplacé`. |
| Same Shift ID, another comparable communication fact changed | `Modifié`. |
| Same Shift ID, all required communication facts proven equal | No delta Shift row. |

For simultaneous same-ID changes, label precedence is **Remplacé → Déplacé → Modifié**. Preserve all additional proven changes in the same semantic row rather than dropping lower-precedence changes or duplicating the Shift.

Compare all relevant captured communication facts, including hours, confirmation, allocation type, outside-schedule state, task, request/contact facts, applicable standard hours, employee-assigned assets, and correlated operational responsibility. Preserve established normalization and effective-publication rules.

Different Shift IDs always mean removal plus addition, even if every other fact is identical. Do not heuristically call that pair a replacement. A Shift moving between projects is removed from the old project and added to the new project; neither message may mix those projects' recipients or disclose the other project's facts.

### 5. Define identity-sensitive P4 fingerprints

P4 uses the established deterministic fingerprint mechanism with an explicit provenance-v4 marker. Its canonical payload covers the week, complete project communication semantics, and deterministically ordered per-Shift tuples containing:

- canonical `shift_id`;
- containing project and date;
- resource identity and communication facts;
- the six new Shift facts;
- existing request/contact, asset, standard-hours, and correlated operational-responsibility semantics.

Retain existing project/header, participant/recipient, and diagnostic semantics needed for current stale protection. A changed Shift-to-fact mapping must change P4 even if aggregate hours, unions, or boolean OR remain unchanged. Removal/addition of different Shift IDs must also change P4 when all other facts are equal.

Requirement IDs remain traceability identifiers; changing one alone must not establish a replacement or new semantic change. An unused phone stored under a legitimate false opt-in must not become published or an effective phone change merely because P4 is introduced.

For v4 only, this decision supersedes ADR-036 sections 2 and 7 insofar as they treat `shift_id` as traceability-only and exclude it from semantic fingerprints. Canonical identity is now necessary evidence for the product distinction between same-Shift replacement and different-Shift removal/addition.

Historical P0/P1/P2/P3 algorithms remain unchanged. Existing historical identity-only compatibility tests remain valid for their historical versions; v4 requires additional tests for the new behavior. Do not use a historical identity-insensitive project comparison or fingerprint shortcut to suppress a v4 Shift delta.

### 6. Use an explicit historical refresh for unavailable facts

For a valid v1/v2/v3 communicated baseline:

1. Preserve every change that the stored snapshot can actually prove. Canonical assignment coverage can prove additions/removals, resource changes, date moves, hours changes, and other captured facts.
2. Compare task descriptions where captured, but never reconstruct a per-Shift historical task identity/code from an aggregate union.
3. For each matched Shift whose required v4 facts cannot be proven, qualify its current state explicitly, for example: **Référence historique incomplète — état courant à reconfirmer**.
4. Show the current unavailable comparison facts without inventing their previous values. An unknown value must never become a fabricated before → after transition.
5. Combine a proven change and historical-refresh qualification in one semantic row. Do not duplicate that Shift.
6. Never describe uncertainty as ordinary `Modifié`. The qualifier means that current facts are being explicitly refreshed, not that a particular historical change was proven.
7. Explain the comparison limitation in preview and generated text/HTML. Never report **Aucune modification** while refresh rows are necessary.
8. If the historical baseline is valid but predates complete assignment provenance, use an explicitly labelled whole-project historical refresh. Do not invent fine-grained matching.
9. If a snapshot is malformed, incomplete for its claimed capability, or falsely complete, fail closed instead of falling back to a refresh.

A v3 → v4 comparison can therefore include otherwise-matching Shifts once as a disclosed historical refresh. This is an explicit transition exception to omitting proven-unchanged v4 Shifts, not permission to repeat them in later complete comparisons.

The transition ends only after a resulting v4 batch becomes COMMUNICATED through the existing workflow. PREPARED, APPROVED, previewed, or Graph-draft-only batches do not complete it. No process-local flag, deployment marker, or rewritten historical snapshot may pretend otherwise.

Thereafter, v4 → v4 comparison omits unchanged Shifts, including unchanged John/Steve when only Mary changes. No Shift changes, no relevant project/header changes, and no required refresh produce zero delta drafts and the explicit no-modifications result.

### 7. Preserve preparation, recipients, content, and delivery

- The explicit full/delta mode must flow through preview and preparation. Full preview remains complete even when a communicated baseline exists. Delta mode requires an eligible communicated baseline and retains `project_planning_change` semantics.
- Preparation validates both the current snapshot fingerprint and the reference baseline used by the delta preview. A changed reference requires regeneration even if the current Planning fingerprint is unchanged. Preserve existing stale checks during preparation, approval, and subsequent workflow actions.
- Every prepared full/delta batch stores the complete current snapshot. The rendered delta and its semantic rows are transient; a filtered body is never itself the baseline payload.
- Once a delta is COMMUNICATED, its full snapshot becomes the next eligible reference through the unchanged latest-communicated selection. Subsequent deltas must not repeat already communicated changes.
- Recipient resolution and diagnostics continue to use complete canonical before/after project contexts. Filtering body rows must not filter recipient authority or create recipient membership from display text.
- Preserve project recipient isolation, old/new resource CC behavior, #879 canonical Resource email resolution, #882 case-insensitive To/CC deduplication with To priority, and generator CC in both modes.
- Header/contact-only project changes must be representable in a project update section without falsely marking every Shift modified.
- Text and HTML derive from one transient semantic representation; no durable semantic tree or independent delta persistence is added.
- Persisted approved subject/text/HTML remain the EML/Graph/SMTP content authority. Transports must not regenerate content from live Planning.
- Preserve #871 presentation, #880 request-owned phone publication, and #881 free subject/body editing. Substantial text edits remain supported with safe server-generated escaped HTML; do not restore mandatory template-equivalence checks or accept arbitrary client HTML as authority.
- Preserve RBAC, explicit approval, audit, prepared-message immutability, and transport idempotency. No automatic sending is introduced.

### 8. Require Architect conformance evidence

The eventual implementation must satisfy this matrix on its exact review head:

| Area | Required evidence |
|---|---|
| Per-Shift capture | Multiple Shifts sharing resource/task/day retain distinct confirmation, allocation type, outside-schedule state, task identity/code/description, and existing facts after JSON round trip. |
| Frozen history | Frozen valid v1/v2/v3 canonical round trips and P1/P2/P3 fingerprints remain unchanged; historical shapes never gain v4 keys or live enrichment. Preserve no-version/P0 compatibility. |
| Capability validation | Builder, raw JSON, and direct construction reject missing/malformed v4 facts, wrong booleans, unknown/downgraded capabilities, duplicate Shift IDs, falsely complete coverage, and v4 facts masquerading as old versions. |
| Matching | Removal, addition, same-ID replacement, date movement, hours/other modification, and simultaneous changes follow precedence while retaining all proven differences. Different IDs remain remove/add, including when all other facts are equal. |
| Lost aggregate changes | Changes or reassignments previously hidden by equal resource totals, confirmation/allocation unions, boolean OR, or task unions change P4 and produce correct Shift deltas. |
| Historical refresh | Valid v1/v2/v3 baselines retain proven changes, qualify missing facts, avoid fabricated old values/transitions and unqualified modifications, and never report false no-change. Pre-v1 refresh is explicitly whole-project; malformed history fails closed. |
| Transition completion | Refresh remains necessary after preview/PREPARED/APPROVED/Graph drafts and ends only after a resulting v4 batch becomes COMMUNICATED. |
| Changed rows only | John/Mary/Steve examples cover removal, same-ID replacement, different-ID removal/addition, movement, and hours changes; unchanged v4 John/Steve rows and unchanged projects are absent. |
| Project isolation | Cross-project movement produces removal/addition with isolated recipients and facts. |
| Project-only facts | Header/contact-only changes are representable without falsely changing all Shift rows. |
| Explicit modes | Full preview stays complete with a baseline; no baseline permits full preview but produces a clear delta-unavailable result; complete equality produces zero delta drafts and the explicit no-modifications message. |
| Communicated lifecycle | PREPARED/APPROVED/Graph-only batches are not baselines; SMTP all-success and truthful manual confirmation establish COMMUNICATED; a communicated delta's full snapshot becomes the next reference without repeating changes. |
| Partial SMTP | Failure remains retryable without becoming a complete baseline; retry skips already SENT messages and preserves delivery audit/idempotency. |
| Stale protection | Reject changed current snapshots and changed reference baselines, including a reference change while the current snapshot fingerprint stays equal. |
| Recipients and regressions | Preserve #871/#879/#880/#881/#882, complete before/after recipient context, old/new resource CC, generator CC, canonical identities, To priority, and case-insensitive deduplication in preview/persistence/transports. |
| Editing and representation | Generated text/HTML agree semantically, XSS values are escaped, #881 free edits remain supported, client HTML is not authoritative, and EML/Graph/SMTP consume the persisted approved pair. |
| Integration | SQL Server JSON persistence and relevant lifecycle validation pass; run all relevant CI on the exact implementation head. SQLite-only evidence or a skipped live SQL Server check is not a substitute where database behavior matters. |

## Alternatives considered

- **Add fields silently to v3.** Rejected because v3 already has a historical serialized-shape and capability contract.
- **Add only confirmation/allocation/outside-schedule fields.** Insufficient because task aggregation also loses canonical per-Shift task correspondence.
- **Infer values from resource aggregates, current tables, names, or similar rows.** Rejected because that fabricates historical facts or identity and can mislabel replacement, movement, or modification.
- **Ignore missing historical facts or mark uncertainty as ordinary modified.** Rejected because silent omission can advance a baseline without disclosing the limitation, while unqualified modification claims an unproven change. Explicit historical refresh preserves the known changes and identifies the unknowns.
- **Persist a second delta store or per-recipient baseline.** Unnecessary for the accepted requirement and outside its lifecycle/authority scope.

## Consequences

### Positive

- The snapshot can prove complete per-Shift changes and same-ID replacement without mutating Planning.
- Historical capabilities remain readable and immutable, with explicit comparison limits.
- The existing full snapshot, baseline selector, approval, audit, and transport lifecycle remain authoritative.

### Trade-offs

- v4 JSON is larger and requires compatible readers, validators, and P4 comparison/staleness handling.
- The disclosed historical refresh can repeat otherwise-unchanged Shifts for one successfully communicated transition generation.
- Different Shift IDs now intentionally produce remove/add in v4 even when other communication facts are identical; heuristic identity reconstruction remains prohibited.
- Older prepared/approved batches may be incompatible/stale and must not be upgraded in place. Older application images are not automatically forward-compatible with v4.

## Implementation notes

### Database and activation boundary

No schema migration, backfill, historical snapshot rewrite, stored-fingerprint rewrite, startup mutation, or scheduled activity is required or authorized by this decision. The existing JSON column stores the widening-only payload. No new baseline table is introduced.

Recording this ADR does not implement #889, activate v4 runtime writes, change roadmap state, or authorize production merge. DEV implementation remains a separate Product Owner handoff. Exact-head Architect conformance review of the eventual implementation PR is required before merge.

### Deployment cutover

Before cutover, resolve outstanding pre-v4 PREPARED/APPROVED batches through the existing workflow, especially partially sent SMTP batches. Preserve their stored content, delivery records, SENT markers, and audit.

Do not recreate partially sent batches automatically, clear delivery history, or replay already SENT messages. Resolve their delivery state before switching to an incompatible fingerprint/runtime contract. Unsent incompatible batches may follow the existing cancel/re-prepare process; do not silently rewrite or promote them.

### Rollback and forward compatibility

Rollback retains v4 JSON and stored fingerprints. Verify the actual immutable PREV image's capabilities; a PREV tag alone does not prove that it can safely read or operate on v4.

If that image cannot safely understand v4, freeze project communication operations until compatible code is restored. Never downgrade snapshots, recompute historical fingerprints to fit old code, or skip the latest communicated baseline in order to select an older compatible snapshot. An unknown/newer capability must fail closed.

Other accepted deployment, SQL Server, data-preservation, and production merge risk gates remain applicable.

## References

- [#889 — Explicit communication mode and Shift-level delta](https://github.com/GroupeLML/ResourcePlanner/issues/889).
- [ADR-036 — Project communication provenance v2](ADR-036-project-communication-provenance-v2.md), retained except for the explicitly scoped v4 Shift identity/fingerprint supersession and v4 comparison behavior defined here.
- [#830 — Request-owned chantier contact projection](ISSUE-830-CHANTIER-CONTACT-PROJECTION.md).
- [ADR-020 — Operational responsibility and approved context](ADR-020-operational-responsibility-hierarchy-and-approved-context.md).
- [ADR-021 — Canonical availability authority](ADR-021-class-scoped-holiday-availability-rules.md).
- [ADR-026 — Asset assignment authority](ADR-026-asset-assignment-authority-and-computed-shift-propagation.md).
- [ADR-011 — SQL Server authoritative database](ADR-011-sql-server-authoritative-database.md).
- [#871](https://github.com/GroupeLML/ResourcePlanner/issues/871), [#879](https://github.com/GroupeLML/ResourcePlanner/issues/879), [#880](https://github.com/GroupeLML/ResourcePlanner/issues/880), [#881](https://github.com/GroupeLML/ResourcePlanner/issues/881), and [#882](https://github.com/GroupeLML/ResourcePlanner/issues/882) — preserved presentation, recipient, phone, editing, and deduplication contracts.
- [AGENTS.md](../../AGENTS.md) and [architecture documentation conventions](README.md).


## Addendum — Forward asset qualification-diagnostic ordering (P01-A-F01, 2026-11-27)

**Accepted by the Product Owner following the Primary Architect's narrow review.** This is a forward acquisition correction for the shared project communication repository (including live v4 and inactive/future v5), not a new snapshot capability or historical fingerprint migration.

The inherited asset scope combined five IN predicates in one unordered SELECT. The accepted persisted fixture produced 2,714 scope parameters plus three fixed predicates (2,717 total), exceeding SQL Server's 2,100-parameter boundary. DEV and Architect reproduced the total and showed that a bounded branch/chunk union retained physical allocation identity and qualification states but could reverse diagnostic encounter order. The unchanged unordered original query could also reverse this order. SQLite execution and MSSQL/pyodbc compilation are not live SQL Server qualification.

After all applicable asset rows have contributed, and before assignment diagnostics are constructed, normalize **only each Shift's asset qualification-diagnostic contribution**: deduplicate exact diagnostic-code strings and sort ascending by case-sensitive ordinal code order. The current known order is `ASSET_QUALIFICATION_MISSING_OPERATOR`, `ASSET_QUALIFICATION_NO_OVERLAP`, `ASSET_QUALIFICATION_SKILL_MISMATCH`. Unknown and future codes follow the same rule. Do not use allocation ID, severity, locale, translation, or case folding; do not sort complete assignment/resource/task/project/global diagnostic arrays. Preserve other diagnostic group concatenation and ordering, qualification rules, applicability, project-context gates, asset ordering, severity and message/blocking semantics.

**Historical compatibility.** P0–P5 historical serialization, deserialization, fingerprint and comparison algorithms, stored arrays, fingerprints and reviewed artifacts remain unchanged. A historical A,B representation and a newly acquired canonical B,A may legitimately differ in P4/P5 and project modified classification despite unchanged underlying facts. Ordering alone must cause zero genuine Shift changes and zero historical-refresh rows. Existing v4 project-context draft behavior may render “Informations du projet mises à jour.” Blocking diagnostics remain effective. Equal canonical-to-canonical comparisons produce no further delta; a genuine qualification-state change remains detectable. Equal canonical v4 business facts versus v5 must not generate version-only Shift refresh. No technical-transition draft, special approval path, automatic send, history rewrite, capability increment or automatically communicated baseline. Latest-COMMUNICATED full-snapshot selection remains unchanged: the older-order snapshot stays authoritative until legitimately superseded through the existing workflow.

**Production promotion prerequisite.** This shared reader correction changes live v4 acquisition when deployed, even before v5 activation. Before the first production promotion containing it, inventory and reconcile outstanding previews, PREPARED and APPROVED batches, external Graph drafts and partially delivered SMTP batches under existing stale protection. A partial SMTP batch must be completed through the compatible existing workflow or promotion postponed pending explicit audited resolution. Do not automatically cancel/recreate batches, replay SENT messages, rewrite history, bypass stale protection or withdraw/change external drafts. Persisted historical exports and reviewed content remain unchanged. This addendum authorizes no present operational action, workflow code change, deployment or database option change. Rollback retains stored arrays and fingerprints; assess current/persisted compatibility and outstanding artifacts before either promotion or rollback.
