# ADR-038 — Emergency broadcasts, user acknowledgments and ephemeral presence

Status: Accepted
Date: 2026-11-20
Architecture gate: ASTRA-894 / Issue #894
Reviewed main: 68c50a73a0e21a7b25df51e71fbd365034cf21b0

## Context

An administrator needs to warn authenticated ResourcePlanner users before urgent maintenance, observe acknowledgments and explicitly end the warning. Notifications and receipts must survive deployment; presence need not. This is an operational feature, not business authority or permission to shut down the application.

The reviewed runtime is React/Nginx → one FastAPI/Uvicorn process → external SQL Server. AppUser is the stable local identity (ADR-012), including the separate break-glass administrator (ADR-014). Existing session resolution reads SQL on every authenticated request. Its synchronous resolver and CSRF guard currently execute directly inside asynchronous authorization middleware. A broadcast cache alone therefore does not remove SQL cost or event-loop blocking.

SQL Server remains the concurrency and persistence reference (ADR-011). Module permissions and sensitive-data isolation remain intact (ADR-030). This feature must neither acquire the Planning mutation lock/version nor interact with ERP scheduling, recovery, leases or checkpoints (ADR-033).

## Decision

### Delivery and ownership

Use a single authenticated, same-origin GET /api/v1/runtime/status per mounted application document. It returns current broadcast identity/content when needed and the caller's receipt, and records only an ephemeral observation of that authenticated caller. This observation is incidental to the read, like an access observation: GET creates no durable event, acknowledgment or business mutation.

Poll with one completion-based timer, independently jittered 4–6 seconds, without overlap. Start immediately after authentication. Keep the same requested cadence in hidden tabs; browser throttling or suspension can delay execution. Coalesce wake/focus/online events and perform a new check within 0–1 second when execution/connectivity resumes. No WebSocket, SSE, external broker, separate heartbeat loop or browser leader election is introduced.

Place the provider and blocking modal below AuthProvider, inside the existing identity reset boundary in main.tsx, above App's normal and detached-Planning branches. Navigation must not recreate polling. Identity changes cancel work and clear state.

The modal is an accessible topmost blocking dialog. Escape and backdrop do not dismiss it. Only a server-confirmed user acknowledgment or a successful authoritative status response showing that broadcast has ended/changed releases it. Preserve underlying drafts. Its acknowledgment does not pause business APIs or prevent subsequent saves.

### Durable state and lifecycle

Create additive SQL tables for immutable broadcasts and receipts. A broadcast has a server UUID, version 1, bounded Unicode title/message, SQL-generated UTC activation time, creator AppUser FK, state ACTIVE/ENDED, optional first end time/actor, and a creator-scoped idempotent creation command identity.

Only one ACTIVE broadcast may exist per ResourcePlanner database. Enforce this with a unique filtered index on state WHERE state = 'ACTIVE'. Concurrent different creations produce one winner and a 409 broadcast_already_active for the loser. Replaying the same creation command returns the existing result; changed content under that key conflicts.

Do not edit active content, silently replace it, schedule activation or implicitly acknowledge for its author. Explicitly end the old broadcast and create a new immutable identity. First successful end wins; retries preserve that actor/time. End/creation success is returned only after commit.

A receipt's primary key is (broadcast_id, user_id), with FKs to the broadcast and AppUser. First successful insertion establishes its canonical SQL-generated UTC timestamp. Receipts are immutable. No mutable acknowledgment counter or second receipt audit stream is added.

### Acknowledgment concurrency

Use insert-first in a small READ COMMITTED transaction. Do not first take update/range locks, use MERGE, serialize the whole broadcast, or acquire a Planning lock. Different users write independent receipt keys. FK/index locks still exist and must be measured; this is not a claim of zero physical contention.

For the recognized receipt-key duplicate (SQL Server 2601/2627), roll back the whole failed transaction, open a fresh transaction and read the committed canonical receipt for the server principal. Return HTTP 200 with that receipt. Do not query in the failed SQLAlchemy transaction or overwrite its timestamp. Other constraint failures are not duplicates.

The retained broadcast may be acknowledged after it ends. This accepts an in-flight/offline retry without locking a shared ACTIVE parent row on every receipt. Late receipt time remains visible; it cannot prove awareness before maintenance.

No software design guarantees absence of every possible database deadlock. A bounded retry policy handles transient SQL victims; mandatory live-SQL acceptance must record zero raw deadlocks in the specified workloads, including duplicates and rollback cases. Retries must not conceal a failing acceptance result.

### Presence and cache

Track one bounded in-memory record per authenticated AppUser.id. No tab, device or session identifier is needed for this product: the maximum last-observed time across all its requests naturally aggregates tabs and PCs. Active means observed within 60 seconds, not human interaction or unsaved-work detection.

Use monotonic expiry, UTC presentation, a 15-minute recent-observation horizon, periodic expiry and a 10,000-user cap. Expose restart warm-up, stale/error and capacity-incomplete states. Never silently present incomplete presence as a complete zero-pending result.

Cache the immutable active-broadcast DTO, including absence, for at most one second with a single-flight loader and generation-safe post-commit invalidation. Bound a positive-only receipt cache to 10,000 keys; missing receipts are queried, never remembered as acknowledged. SQL remains authoritative. Caches reset safely on restart.

Presence assumes exactly one serving backend process for this deployment. Multiple workers/replicas require a separate architecture decision for shared ephemeral presence; sticky sessions alone do not provide a complete administrative roster. SQL uniqueness/idempotency already applies across connections/processes.

### Authorization and runtime cost

All authenticated AppUsers may read status and acknowledge only themselves. Reuse admin_settings for creation, ending, presence and history; this permission is currently ADMIN-only. Explicitly classify the new routes before generic GET/mutation fallbacks. Keep ordinary users' status separate from all-user administrative data. POST commands retain session/CSRF protection.

Use AuthPrincipal.local_user_id exclusively for durable actors. No browser-supplied user/actor identity, Resource identity, ERP identity, name/email matching or synthetic production user is accepted. Local development already materializes a persisted bootstrap AppUser; fixtures must provide real AppUsers.

Render title/message as plain escaped React text. Do not accept HTML/Markdown or use an HTML injection API.

Keep synchronous SQL/auth/CSRF work off the event loop with preserved authorization semantics and thread-owned sessions. Bound feature admission, SQL work, caches and logs. No per-heartbeat SQL writes, persistent last_seen, business aggregates, extra SQL pool or provider call is allowed. Preserve existing normal-API performance budgets and verify the mixed workload before promotion.

### Operations and rollout

Retain ended broadcasts and their receipts for 90 days; never expire an active warning automatically. A separate bounded lifespan maintenance task prunes only this feature's expired data. It is independent of the ERP scheduler and never changes business state.

Network/server failures preserve already displayed unacknowledged content and do not automatically log users out. Failed receipts remain pending. SQL unavailability cannot be converted to an acknowledgment or a fresh empty broadcast response.

Use a forward additive migration, no backfill and no automatic downgrade. The existing readiness probe requires the exact repository Alembic head; an unchanged PREV image will fail readiness after the new migration even though its business tables are compatible. Before feature promotion, prepare and test a rollback bundle containing prior application behavior plus knowledge of the new migration lineage, leaving new tables/data in place. Do not weaken exact readiness globally. A rollback without this feature preserves its data but temporarily loses delivery/receipt functionality.

## Alternatives considered

- WebSockets/SSE: unnecessary persistent-connection/proxy/reconnect complexity for the current scale; neither solves suspended-browser execution or SQL idempotency.
- Durable heartbeat writes or a Redis deployment: not justified for single-process ephemeral presence.
- Per-tab receipts or client-only deduplication: cannot satisfy one receipt per user across devices.
- Broadcast-wide serialized upsert/count updates: creates avoidable shared-row contention.
- In-place edits with unchanged receipts: cannot establish that the revised message was seen.
- Assuming additive schema makes unmodified PREV ready: contradicted by current readiness.py.

## Consequences

The architecture is accepted for bounded DEV planning; it is not implementation, merge, deployment or roadmap authorization. One implementation WorkItem/PR, executed in four separately authorized phases, is recommended. Real SQL Server concurrency, mixed-load performance, browser accessibility/failure acceptance and a tested rollback bundle remain delivery gates.

The exact schema/API, races, limits, quantitative estimates, validation thresholds, phasing and operational handoff are normative in [ASTRA-894 implementation contract](ASTRA-894-implementation-contract.md).

## References

- Issue #894; AGENTS.md sections 3B, 3C, 12, 13, 18, 19 and 23.
- ADR-011, ADR-012, ADR-014, ADR-030 and ADR-033.
- app/server/runtime.py; app/server/security.py; app/server/oidc.py; app/server/readiness.py.
- app/infrastructure/sql/auth_session_repository.py; app/infrastructure/sql/session.py.
- frontend/src/main.tsx; frontend/src/App.tsx; docker-compose.yml.
- [SQL Server filtered indexes](https://learn.microsoft.com/en-us/sql/relational-databases/indexes/create-filtered-indexes).
- [SQL Server deadlocks guide](https://learn.microsoft.com/en-us/sql/relational-databases/sql-server-deadlocks-guide).
- [FastAPI concurrency](https://fastapi.tiangolo.com/async/).
- [Browser visibility and timer limits](https://developer.mozilla.org/en-US/docs/Web/API/Page_Visibility_API).
