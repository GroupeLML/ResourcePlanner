# ADR-039 — Mandatory superintendent CC and recipient-policy provenance v5

Status: Accepted

Date: 2026-11-25

Architecture gate: ASTRA-869

WorkItem: #869

Reviewed main: a8f6d838887c2866ac69669298e55b731792311a

Decision authority: explicit human Product Owner acceptance of the ASTRA-869 architecture, the project-only legacy cutover decision, the no-recipient-only-delta decision, and the employee-email correction workflow recorded below.

## Context

The coordinator requires every active employee holding the canonical Surintendant competency to receive every ResourcePlanner scheduling communication, regardless of project membership or assignment. Inclusion is automatic and mandatory. Preparation, approval and external delivery remain explicit.

Issue #290 deliberately confined CC recipients to each project's team. Issue #869 explicitly authorizes a narrow exception for these superintendents. It does not authorize broader Planning, project, demand or communication-module access, multi-project email bodies, or changes to unrelated notifications.

Current project communications use canonical project/contact and Resource identities, immutable project snapshots, explicit full/delta modes and persisted approved recipients. ADR-036 and ADR-037 define cumulative provenance and historical compatibility. Current v4/P4 does not capture an independent global superintendent recipient-policy context. A transport-only CC addition would leave eligibility changes outside snapshot staleness and historical provenance.

The canonical API also still exposes older scheduling generation and Graph-draft actions that produce technician/manager messages outside the project-based workflow. The Product Owner has accepted project-only new scheduling generation at cutover, while preserving historical records, shared capabilities and authorized access.

## Relationship to accepted contracts

This ADR makes a forward-looking exception to #290's project-team-only CC rule and extends ADR-037 with recipient-policy capability v5/P5. The existing `project_v2` envelope, per-Shift authority, latest-communicated baseline, approval and delivery lifecycle remain applicable.

Historical P0/P1/P2/P3/P4 serialization and fingerprint algorithms remain unchanged. ADR-036's assets, standard hours, immutable prepared content and persisted text/HTML authority remain applicable. ADR-037's genuine Shift/project/contact delta semantics and historical-refresh rules remain applicable except for the explicit forward extensions in this ADR.

Preserve #593 Resource identity, #879 Resource.email resolution, #880 explicit request-owned phone publication, #881 free subject/body editing, #882 To-over-CC deduplication and #892 presentation safeguards. ADR-007 keeps human Resources distinct from assets. ADR-012, ADR-017, ADR-027 and ADR-030 retain their identity and authorization boundaries.

## Decision

### 1. Scope and project-only generation

The current Communications page and both project-based modes remain supported:

- full project confirmation;
- explicit delta since the latest eligible communicated baseline for the same week.

Every newly composed included project message must apply the superintendent CC policy. The existing explicit choice to exclude a whole project message from a batch remains available; exclusion must not become a way to remove mandatory recipients from an included message.

Retire only new legacy scheduling generation and legacy external-draft actions that could bypass project-based recipient composition. New scheduling output must not fall back to technician/manager legacy generation. Preserve one message per project and do not aggregate unrelated projects into one email.

At the reviewed main, this retirement targets legacy `GET /api/v1/communications/preview`, `POST /api/v1/communications/batches` and `POST /api/v1/communications/batches/{batch_id}/create-drafts`, together with the corresponding application-service entry points so that another caller cannot bypass the policy. It does not retire the project-based routes, contact listing/editing or authorized historical batch reads and explicit settlement actions. Any retained lifecycle operation must preserve audit and cannot generate new legacy output.

Do not delete the current Communications page, shared communication tables, contact-management capabilities, historical batches/messages/snapshots/audit/delivery records, or shared services merely because they originated in the legacy workflow. Preserve authorized historical access. Any retained historical resolution/cancellation operation remains explicit and audited and must not create fresh legacy scheduling output.

The rule applies only to scheduling communications. Do not add these recipients to demand or approval notifications, emergency broadcasts or unrelated messages.

### 2. Canonical employee and competency authority

Eligibility is determined from canonical human `Resource` identities:

- `Resource.active` is true;
- `Resource.erp_active` is true;
- canonical `ResourceCompetency` membership points to the configured Surintendant `Competency.id`.

Do not require project involvement, a Shift, availability on the communicated dates, an AppUser, a previous login, an employee display-name match or a security role. Do not infer membership from the legacy `Resource.competencies` text, resource-class labels, approximate names or unrelated qualifications. The current membership model has no proficiency or expiry facts; this decision introduces none.

The future Surintendant security role in #901 is independent. This feature neither requires nor assigns it and must not infer competency membership from that role.

Bind the recipient policy server-side to one verified, stable `Competency.id`, rather than resolving a mutable label silently on every composition. Verify the actual intended catalogue identity in each deployment environment; no production ID is assumed by this ADR. A one-time server configuration binding is not a per-message selection or opt-in and must not provide an off switch for mandatory inclusion.

A rename of the bound competency preserves its identity and membership semantics. A missing, deleted or inactive bound definition is a blocking configuration diagnostic for new scheduling composition. Duplicate or ambiguous catalogue definitions must never be resolved by arbitrary first-match selection, approximate matching or unioning multiple definitions. Do not automatically rebind to another row with a similar or newly reused label.

A valid active bound competency with zero eligible members is distinct from a missing/invalid binding: report the zero population explicitly. Legacy text-only membership anomalies require correction through the canonical master-data workflow; no automatic membership backfill is authorized.

Configuration failures must not grant permissions or disable unrelated application operations.

### 3. Address authority, identity deduplication and mandatory blockers

Resolve each eligible employee from `Resource.id` and its explicitly maintained canonical `Resource.email`, following the resource-rooted policy from #593/#879. No AppUser/login dependency or new superintendent-specific email field is introduced.

Preserve existing BusinessContact precedence for manager/contact-rooted recipients and the existing stable-identity Resource.email fallback where applicable. Do not introduce name matching, email matching as identity authority, or a reverse fallback that silently substitutes another person's contact.

Preserve existing legitimate To and CC recipients, complete before/after project recipient context for deltas, and optional generator CC. Deduplicate known canonical identities using existing stable links, then deduplicate trimmed addresses case-insensitively with To priority. Do not merge people by display name. Distinct identities sharing an address retain their contributing identities/roles in provenance even though the transport address occurs once.

An eligible superintendent already represented by a valid, canonically linked To recipient does not need a duplicate CC copy. Record that the existing recipient fulfills the policy; do not overwrite the established To address authority.

Validate usable single-recipient addresses before preparation, including rejection of invalid syntax, header injection and ambiguous address lists. Never fabricate an address, silently drop a required recipient, substitute another identity, truncate the eligible population or add an unapproved recipient override.

An eligible mandatory employee without a usable, unambiguous address is a blocking diagnostic for affected included messages unless a valid existing recipient already fulfills delivery to the proven identity. Preserve established behavior for unrelated nonmandatory assigned-resource warnings; the new mandatory population must not be silently downgraded to warning-only omission.

### 4. Actionable employee-email correction workflow

The preview must identify every affected eligible employee and the specific missing, invalid or ambiguous address reason. A generic batch error alone is insufficient. Use the employee's display name and canonical resource identity for unambiguous identification, including where names are duplicated.

Provide actionable French diagnostics. For example, with placeholders rather than real employee data:

> Surintendant « {nom_affiche} » (ressource {resource_id}) : adresse courriel absente. Renseignez le courriel dans la fiche employé existante, enregistrez, puis régénérez la prévisualisation.

Use an equally specific reason for an invalid or ambiguous address. A grouped diagnostic may avoid repeating the same employee on every project, but it must retain all affected employees and the blocking relationship to included messages.

The required coordinator workflow is:

1. Generate the scheduling communication preview.
2. Identify each eligible superintendent with an unusable address and the reason.
3. Open the corresponding existing employee information card using canonical identity.
4. Enter or correct the existing employee email field and save through its normal authorized workflow.
5. Regenerate the communication preview.
6. Resolve fresh canonical `Resource.email` data and automatically include the employee, applying normal identity/address deduplication.
7. Permit normal preparation, approval and delivery once all blocking conditions are resolved.

No application restart is required. No stale process-lifetime cache may hide a committed employee-email correction. Regeneration must obtain fresh canonical recipient data through the composition read.

The diagnostic/navigation does not grant employee-administration permission. Reuse existing access checks; a coordinator without editing rights must obtain correction from an authorized employee administrator. Do not embed a privileged editor or a second email field in Communications.

Correcting an employee record does not mutate existing prepared/approved messages, silently change their recipients or perform automatic regeneration/preparation/approval/sending. Those artifacts remain frozen and follow the existing stale/cancel/re-prepare workflow where necessary.

### 5. Immutable recipient-policy provenance v5/P5

Keep `communication_batches.model_version = project_v2` and the existing full `project_snapshot_json` payload. Introduce cumulative snapshot capability `request_provenance_version = 5` (v5), with P5 fingerprints. This adds recipient-policy facts, not a new source of Shift or Planning facts.

Every current v5 projection must preserve all v1-v4 facts and an explicit versioned recipient-policy context containing:

- policy identity and verified competency binding;
- the complete eligible population's canonical Resource identities;
- resolved recipient identities, address authority and resolution facts;
- enough source-role information to retain identity provenance through To/CC/address deduplication;
- explicit population-capture completeness and relevant diagnostics.

Capture the global context once per complete snapshot; preserve each message's effective recipient authority and contributing provenance. Do not duplicate superintendent membership as fake project membership, request assignment or Shift rows.

Population completeness and address usability are separate. A fully enumerated population with a missing email must retain that employee and its blocking resolution diagnostic, rather than becoming an apparently complete empty or smaller roster.

Compose the final To/CC policy before message/content fingerprinting and manual content review. P5 must cover the recipient-policy identity, eligibility/resolution context and deterministic canonical ordering in addition to the existing v4 project/Shift semantics. Relevant changes must participate in current-snapshot stale and duplicate protection even when they do not generate a delta.

Preserve all historical P0-P4 canonical serialized shapes and algorithms exactly. Do not inject v5 fields into an older version, relabel old facts as v5, rewrite stored fingerprints or enrich old snapshots from current live Resources or competencies. Validate capability/completeness consistently in construction, serialization, deserialization, comparison and fingerprinting. Missing, malformed, unknown, falsely complete or downgraded capabilities fail closed. V5-only facts masquerading as an older capability must be rejected.

### 6. Full/delta behavior and baseline authority

Apply the current mandatory population whenever a full or otherwise-required project delta message is generated. This includes a removal message whose project context comes from the historical snapshot. Do not union historical global superintendent populations into today's mandatory population.

A change confined to the global superintendent population, its binding/resolution context or its email addresses must not independently select a project for delta generation. It creates no recipient-only draft and no fabricated Shift modification. The user may explicitly request a full communication through the existing mode.

Preserve genuine project/header/contact and per-Shift changes already defined by ADR-037. For example, a resource's address change can independently affect its existing assigned-resource communication facts; that existing semantic change must not be suppressed merely because the person is also a superintendent.

Keep recipient-policy staleness separate from business delta selection: a changed P5 context may invalidate an earlier preview/batch without proving a Shift change or requiring a message for every project.

A valid v4-to-v5 transition alone does not require Shift historical-refresh rows: v4 already captured the relevant Shift facts. Preserve ADR-037's explicit refresh behavior for historical capabilities that lack those facts. Never invent previous recipient-policy facts or before/after values for history that did not capture them.

Keep a complete immutable current snapshot in every prepared full/delta batch. Retain the latest `COMMUNICATED` same-week snapshot selector, including communicated deltas. Previewed, PREPARED, APPROVED and Graph-draft-only batches do not become communicated baselines. Preserve current and reference-baseline stale checks. No per-recipient or partial-project baseline, new baseline table or automatic catch-up message is introduced.

### 7. Approval, stale state and external delivery

Prepared/approved recipients, subject, text and HTML are immutable delivery artifacts. Existing explicit approval and external-action boundaries remain authoritative.

| Change timing | Required behavior |
| --- | --- |
| Before preview | Use current canonical eligibility and recipient data. |
| Between preview and preparation | Reject a stale preview; regenerate rather than repairing the old review silently. |
| Between preparation and approval | Reject stale approval; stored messages remain unchanged. |
| After approval, before an external action | Apply existing current-snapshot protection; never silently retarget approved messages. |
| During an external action | Preserve the approved envelope; already handed-off mail cannot be revoked by changing eligibility. |
| Between partial SMTP delivery and retry | Unchanged context retains retry/skip-SENT behavior; changed context follows existing stale protection and explicit audited resolution. |
| After communication | Preserve the historical snapshot, recipients, content, delivery records and baseline. |

Do not automatically recreate a partial batch, replay SENT messages, erase delivery history, mark a partial batch fully communicated or bypass stale checks to resume it. Any necessary operational resolution remains explicit and auditable.

Graph continues to create drafts only. SMTP remains an explicit send action after approval. EML, Graph and SMTP use the same persisted approved recipient and content authority without live recipient re-resolution. Preserve idempotency, delivery claims, canonical message identities, audit, text/HTML agreement, #880 phone selection and #881 free editing with safe server-generated HTML.

ResourcePlanner controls its persisted envelopes and draft creation; subsequent manual edits or sending in Outlook remain outside that authority. A Graph draft is not proof of sending. Eligibility is observed and checked at existing workflow checkpoints, not atomically locked across external email delivery.

### 8. Confidentiality and authorization boundary

The cross-project superintendent exception is intentional and confined to scheduling distribution. Every message still contains only its project's scheduling context. Visible To/CC addresses and the authorized project content are disclosed to those recipients; preview must make the automatic superintendent CC population clear before approval.

Email inclusion grants no Planning, project, demand, employee-administration or communication-module permissions. Preserve existing RBAC and authorized historical access. Client requests cannot supply arbitrary policy bindings, employee identities or recipient overrides.

Do not automatically add unrelated confidential information or combine projects. Preserve free text editing and explicit coordinator review without reintroducing mandatory template equivalence or accepting arbitrary client HTML. Do not expose recipient/address diagnostics through unauthorized surfaces or introduce raw personal-data logging.

### 9. SQL/read efficiency and runtime boundaries

Use a complete canonical eligibility read, bounded independently of the number of projects/messages. Prefer the configured competency identity and active Resource/ResourceCompetency joins, with batched stable-identity resolution where needed. Do not execute the same global population lookup once per project, message or transport, use the catalogue search's default result limit, or silently truncate recipients.

Capture one coherent recipient context per composition/projection read within the existing unit-of-work boundary. Preserve existing revalidation points, including re-reading/comparing before persistence where required. Do not add long-lived cross-request eligibility caches, global employee locks or a transaction spanning external mail delivery.

No extra SQL engine/pool, worker change, polling loop, scheduled sending task, Planning mutation/version change, ERP mutation or unrelated provider lookup is authorized.

### 10. Persistence, cutover and rollback

No database schema migration, historical backfill, new baseline table or startup data rewrite is required by this decision. Existing JSON persistence carries v5 and recipient provenance. The new server-side competency binding must be supplied and verified before feature cutover; this ADR records no production ID or operational readiness assumption.

Before production cutover, require evidence of:

- the actual production competency-ID binding, canonical membership quality and actionable address diagnostics;
- outstanding legacy scheduling drafts, including relevant external Graph drafts and potential external consumers of legacy API generation;
- incompatible PREPARED/APPROVED project batches, especially partially sent SMTP batches;
- an explicit resolution plan that preserves historical content, audit and SENT/idempotency records;
- availability of compatible v5 readers/writers and the existing current Communications page/full/delta workflows.

Retire legacy generation narrowly. Do not delete shared data/services or assume legacy API consumers are absent. Preserve necessary authorized historical access. Do not resolve outstanding artifacts by silently rewriting, converting or recreating them.

Rollback preserves v5 JSON, fingerprints and approved recipient artifacts. Verify the actual immutable PREV image's capabilities. If it cannot safely interpret or operate on v5, freeze project-communication operations until compatible code is restored. Never downgrade snapshots, omit recipients silently or skip the latest communicated baseline to select an older compatible one. Existing deployment, manual merge and production-risk gates remain applicable.

### 11. Required implementation and acceptance evidence

Implementation remains DEV-owned and requires two separately authorized phases on the eventual implementation PR:

| Phase | Objective and required checkpoint |
| --- | --- |
| 1 — canonical resolution and provenance | Competency binding, complete eligible Resource selection, canonical address/identity rules, v5 construction/validation/serialization/fingerprints and frozen historical compatibility, with focused regression proof. |
| 2 — workflow and delivery integration | Full/delta composition, actionable employee correction/regeneration UI/API behavior, narrow legacy cutover guards, stale/lifecycle/transport integration and complete SQL/browser regression evidence. |

These are checkpoints, not releases or permission to deploy an intermediate phase. Final exact-head Architect conformance review is REQUIRED before implementation merge, followed by the applicable PO merge-risk and manual-merge process.

The implementation must establish at least:

| Area | Required executable evidence |
| --- | --- |
| Global selection | Eligible employees assigned to the project and not assigned to it are included across unrelated projects; local inactivity, ERP inactivity and absent membership are excluded independently. |
| Canonical authority | No AppUser/login or #901 role dependency; no text/name inference; stable-ID rename behavior; missing/inactive/ambiguous binding fails closed; valid zero population is explicit. |
| Recipient preservation | Existing To/CC and optional generator preserved; canonical-identity and normalized-address deduplication with To priority; distinct identities/roles remain traceable. |
| Address failures | Every missing/invalid/ambiguous eligible employee is identified by actionable French diagnostics and canonical identity where needed; no fabricated address, silent omission or manual override. |
| Correction/regeneration | Preview shows the employee-specific blocker; an authorized user opens the existing employee card, saves canonical Resource.email, and regenerates without restart; the fresh preview automatically includes the employee, clears the resolved blocker and permits normal preparation/approval/delivery once all blockers are resolved. |
| Permission preservation | A coordinator without employee-edit permission receives no new authority; the existing administrator correction path remains available; no unrelated read or RBAC expansion. |
| Frozen artifacts | Employee correction and regeneration do not mutate prior PREPARED/APPROVED messages; stale/cancel/re-prepare behavior and historical exports preserve their stored recipients. |
| Full/delta | Both modes apply the current global population, including removed-project messages; global roster/address-only changes create zero independent delta drafts; genuine existing project/Shift/contact changes remain correct. |
| Historical capability | Frozen P0-P4 canonical round trips/fingerprints remain unchanged; v5 round trips and rejects invalid capability claims; no live historical enrichment or unnecessary v4 Shift refresh. |
| Staleness | Changes after preview/preparation/approval invalidate the appropriate actions; changed delta reference still fails even if current Planning facts are equal. |
| Delivery and editing | Preview/prepared recipients and content match EML/Graph/SMTP; Graph stays draft-only; SMTP stays explicit; phone, free subject/body edits, safe HTML and presentation remain correct. |
| Partial delivery/audit | Retry skips SENT messages, preserves claims/idempotency/audit and never silently repairs recipients or establishes a partial baseline. |
| Legacy retirement | Canonical API generation cannot bypass the project-only policy; current Communications page/full/delta still work; contact management, shared data/services and authorized historical access remain intact. |
| Query completeness | No per-project/message population N+1, search-limit truncation or hidden population cap; query/parameter batching remains SQL Server-compatible. |
| SQL Server | Real engine evidence for selection, relevant collation/identity behavior, JSON recipient/snapshot persistence and changes across independent transactions/lifecycle checks; SQLite-only fixtures or a skipped/no-op live job do not establish this coverage. |

Keep all relevant current regression suites and add explicit live-SQL CI selection/module wiring for #869 during implementation. No external email is sent merely to establish unit/SQL/transport-contract coverage.

## Alternatives considered

- Transport-only or post-fingerprint CC injection: rejected because it bypasses recipient provenance, stale protection and preview-to-delivery consistency.
- Eligibility from role #901, names, legacy text or current project assignments: rejected because it establishes another eligibility source or excludes explicitly eligible employees.
- Warning-only omission or a superintendent-specific email override: rejected because mandatory inclusion and canonical Resource.email correction would be bypassed.
- Recipient-only delta messages or fake Shift modifications: rejected by the Product Owner; roster changes affect future legitimate composition and stale state, not independent scheduling message generation.
- Silent v4 extension or historical enrichment: rejected because historical serialized shapes/fingerprints and unknown historical recipient facts must remain trustworthy.
- Deleting legacy tables, contact management or shared services: rejected; retirement concerns fresh legacy scheduling output and external-draft actions, not historical data or shared capabilities.

## Consequences and authorization

The accepted architecture intentionally widens scheduling email distribution while keeping application access controls, project isolation, explicit approval and persisted delivery authority intact. Invalid mandatory-recipient data can block new scheduling preparation until corrected through the existing employee workflow.

V5 adds new JSON capability and recipient provenance and requires compatible readers, deterministic fingerprints, cutover handling and verified rollback behavior. Production impact is sensitive because email disclosure and external delivery cannot be undone by editing local records.

Recording this ADR does not implement #869, activate v5 writes, configure a production competency, establish operational cutover readiness, retire an API at runtime, mark ASTRA-869 DONE, promote #869, authorize DEV phases, perform final conformance or PMRA, or authorize Ready/merge/deployment. Manual Product Owner orchestration remains in effect. #894/PR #897 and unrelated work remain outside this recording turn.

## References

- [#869 — mandatory superintendent CC](https://github.com/GroupeLML/ResourcePlanner/issues/869).
- [#290 — project scheduling communications and original project-team confidentiality](https://github.com/GroupeLML/ResourcePlanner/issues/290).
- [#593 — canonical Resource recipient identity](https://github.com/GroupeLML/ResourcePlanner/issues/593).
- [#879](https://github.com/GroupeLML/ResourcePlanner/issues/879), [#880](https://github.com/GroupeLML/ResourcePlanner/issues/880), [#881](https://github.com/GroupeLML/ResourcePlanner/issues/881), [#882](https://github.com/GroupeLML/ResourcePlanner/issues/882), [#889](https://github.com/GroupeLML/ResourcePlanner/issues/889) and [#892](https://github.com/GroupeLML/ResourcePlanner/issues/892) — retained recipient, phone, editing, delta and presentation contracts.
- [ADR-007](ADR-007-reservable-non-human-resources.md), [ADR-012](ADR-012-preprovision-app-users-before-oidc-link.md), [ADR-017](ADR-017-erp-project-manager-and-rp-co-managers.md), [ADR-027](ADR-027-planning-visibility-role-and-authorized-scope.md) and [ADR-030](ADR-030-module-read-capabilities-and-indirect-data-access.md).
- [ADR-036](ADR-036-project-communication-provenance-v2.md) and [ADR-037](ADR-037-project-communication-provenance-v4-and-shift-deltas.md).
- [#901 — independent future security-role requirement](https://github.com/GroupeLML/ResourcePlanner/issues/901).
- [AGENTS.md](../../AGENTS.md) and [architecture documentation conventions](README.md).


### Forward asset qualification diagnostic ordering — ADR-037 addendum

The PO-approved [ADR-037 forward asset qualification-diagnostic ordering addendum](ADR-037-project-communication-provenance-v4-and-shift-deltas.md#addendum--forward-asset-qualification-diagnostic-ordering-p01-a-f01-2026-11-27) applies to the shared originating business acquisition for v5 as well as live v4. Its per-Shift exact-code normalization, disclosed historical P4/P5 ordering transition and first-production-promotion prerequisites are authoritative. No historical normalization, new capability, changed P5 algorithm, altered recipient authority, version-only Shift refresh, stale bypass, automatic send or changed latest-COMMUNICATED baseline selection is authorized. All other ADR-039 decisions remain unchanged.
