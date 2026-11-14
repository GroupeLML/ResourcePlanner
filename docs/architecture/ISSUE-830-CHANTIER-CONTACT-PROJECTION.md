# #830 — Request-owned chantier contact communication projection

Accepted Architect review: B — ACCEPT WITH CONDITIONS (2026-11-13).

- Authority: `WorkforceRequest.site_contact_name` and `site_contact_phone`; no project, ERP or BusinessContact contact authority.
- `ProjectCommunicationRequestAssignment` snapshots one provenance record per physical Shift. The Shift and requirement IDs are traceability only; demand ID and employee/resource ID determine communication mapping. Aggregated hours must not be double-counted.
- The JSON capability boundary `request_provenance_version=1` is distinct from `request_provenance_complete=True`; the latter proves all participating Shifts are represented, including genuine ad-hoc Shifts whose request/contact values are null.
- Current preview/preparation/stale fingerprint uses P1 (legacy semantic P0 plus request-to-resource/contact semantics), excluding physical identity-only churn from delta semantics.
- Legacy *communicated* baseline: if current project has no nonblank chantier contact, compare P0; if any contact is configured, mark project modified. Complete baseline vs complete current uses P1. Inconsistent current provenance fails closed.
- `communication_batches.model_version` remains `project_v2`. Persisted JSON is additive, with explicit capability validation. No communication-table migration. Historic batches are read from immutable JSON, never enriched from mutable current demands, and stored fingerprints must not be rewritten.
- Prepared bodies/snapshots remain immutable. Contact changes make previously prepared batches stale. Legacy prepared batches with incompatible fingerprints must be cancelled and prepared again.
- One project email (`project:<project_id>`), established recipient To/CC (including requester), delivery and idempotency behavior remain unchanged.
- Migration `0035_workforce_request_site_contact` adds two nullable columns; historical rows remain null. Do not infer contact information.
- **Rollback safeguard:** retain the forward SQL schema. Confirm the **actual** PREV image immutable digest and code capabilities before rollback; a `PREV` tag alone does not prove compatible provenance-aware snapshot/fingerprint functionality. If the PREV image lacks capability 1 and current P1 fingerprint handling, **freeze all project communication operations (preview, prepare, approve, deliver, regenerate, and snapshot writes)** until a compatible image is restored; do not use old approval/delivery paths to recompute or rewrite fingerprints. Do not automatically downgrade or drop potentially populated contact columns.
- SQL Server is the authoritative validation environment under ADR-011; a skipped live SQL Server test is not a pass.
- Frozen SQLite 0048 bridge/cutover schema needs explicit compatibility validation and may not have the new contact columns. Do not relax unrelated schema checks.
- Final typography, mobile layout, hour/asset redesign and exact emphasis are owned by #855; ERP manager telephone is owned by #831.
