# ASTRA-894 — Emergency broadcast implementation and acceptance contract

Date: 2026-11-20
Architecture: [ADR-038](ADR-038-emergency-broadcasts-and-ephemeral-presence.md), Accepted
Issue: #894
Reviewed main: 68c50a73a0e21a7b25df51e71fbd365034cf21b0

This document defines the accepted implementation target. None of the new runtime behavior, schema, tests or performance results described below exists merely because this architecture gate is accepted. The Product Owner must authorize DEV phases separately. Do not start #860, change roadmap structure, implement this contract during the gate, or infer merge/promotion permission.

## 1. Evidence and boundaries

| Reviewed source at the stated main | Relevant finding |
| --- | --- |
| AGENTS.md §§3B, 3C, 12, 13, 18, 19, 23 | Phased DEV, exact-head review/CI/PMRA, live SQL evidence, manual merge and durable architecture |
| ADR-011 | SQL Server is the reference for constraints, isolation, migrations and performance |
| ADR-012; security.py; identity_models.py | Stable AppUser.id; active account and roles are server authority |
| ADR-014; break_glass.py; oidc.py | Break-glass uses the same AppUser/session/CSRF path without contacting OIDC |
| ADR-030; server/security.py | Explicit endpoint capabilities; no business-module data leakage |
| ADR-033; server/http.py lifespan | Existing ERP-owned scheduler/drain/recovery must stay independent |
| server/runtime.py run_server; Dockerfile.backend; docker-compose.yml | One constructed ASGI application, one Uvicorn process, one backend service; no configured replicas/workers |
| sql/session.py; sql/auth_session_repository.py; sql/identity_repository.py | Shared engine; session → AppUser → optional BusinessContact means 2–3 SELECTs per session-authenticated request |
| server/security.py resolve_principal/_csrf_allowed; oidc.py | Synchronous SQL resolvers/guards currently called directly from async middleware |
| frontend/main.tsx; App.tsx | Identity-keyed application lifetime; module tabs; detached Planning returns before the normal shell |
| server/performance.py | Existing auth/DB/compute/serialization timings; synchronous per-request JSONL logging |
| server/readiness.py; DEPLOYMENT_UBUNTU_VM.md | Exact Alembic-head equality; explicit promotion and rollback compatibility check |
| migrations/versions/0037_workforce_request_project_manager_phone.py | Current migration head is 0037; no broadcast tables |
| syntax-check.yml; live SQL concurrency tests | Existing SQL Server job has title-based opt-in and an explicit test list; #894 is absent |

Do not load Planning, Shifts, Demands, WorkPackages, ERP snapshots or ViewScope projections to answer runtime status. Do not call the business facade, increment Planning mutation/version state, acquire its lock, or reuse the ERP occurrence/control row. Server HTTP/composition owns infrastructure wiring; application/domain owns broadcast policy and ports; SQL-specific mechanics remain in infrastructure.

Repository evidence confirms a canonical topology, not a live inventory of operator hosts. Verify the actual target still has one serving backend process before deployment.

## 2. Durable schema and migration

Add a new forward Alembic revision after the actual current head at implementation time. Reserve no migration number in this design: 0038 would be next at the reviewed main. Do not edit a shared historical revision.

### emergency_broadcasts

| Field | Contract |
| --- | --- |
| id | Server-generated UUID stored with the existing String(36) ID convention; primary key |
| version | Integer 1, non-null, check version = 1; content is immutable in this release |
| title | Unicode/NVARCHAR(160), non-null, trimmed nonblank |
| message | Unicode/NVARCHAR(4000), non-null, trimmed nonblank; plain text with line breaks |
| created_at, activated_at | SQL-generated UTC DateTime(timezone=True)/DATETIMEOFFSET; same value for immediate activation |
| created_by_user_id | Non-null FK to app_users.id |
| creation_command_id | Validated client command UUID; idempotency only, never actor authority |
| creation_payload_hash | Server hash of the normalized title/message/version, for replay conflict detection |
| state | Non-null string ACTIVE or ENDED, SQL CHECK |
| ended_at, ended_by_user_id | Both absent while ACTIVE; both present while ENDED, with end actor FK and ended_at >= activated_at |

Use a named UNIQUE(created_by_user_id, creation_command_id) and a named unique filtered index on state WHERE state = 'ACTIVE'. The latter is the database-enforced single-active invariant, including two administrators/processes. Define matching SQLite partial-index behavior for fast tests, without claiming SQLite proves SQL Server behavior.

Index ended history for bounded retention/history pagination, for example (state, ended_at, id). SQL Server filtered-index SET options must be correct on migration and runtime writer connections and verified against the actual ODBC profile. No trigger or automatic update timestamp is needed.

Validate lengths as UTF-16 code units, including astral characters, so API/React validation agrees with SQL Server NVARCHAR limits. Use Unicode types, parameterized SQL and no stored HTML. The request body is bounded independently of field validation.

### emergency_broadcast_acknowledgments

| Field | Contract |
| --- | --- |
| broadcast_id | String(36), non-null FK to emergency_broadcasts.id |
| user_id | String(36), non-null FK to app_users.id |
| acknowledged_at | Non-null SQL-generated UTC DateTime(timezone=True)/DATETIMEOFFSET |

Use PRIMARY KEY(broadcast_id, user_id); it supplies the unique guarantee and broadcast-scoped count/list lookup. No surrogate receipt is necessary. Use SQL Server UTC defaults, for example an infrastructure-local expression equivalent to TODATETIMEOFFSET(SYSUTCDATETIME(), '+00:00'), not browser time or a timezone-dependent GETDATE.

All FKs use NO ACTION; no cascade from AppUser or any business aggregate. The receipt is the durable acknowledgment evidence. No duplicate generic audit event per retry, counter on the broadcast, poll event table, recipient snapshot or durable presence table is introduced. Broadcast creator/end fields themselves preserve the lifecycle actors.

The migration only adds these tables, constraints and indexes. It creates no users, broadcasts or receipts, modifies no existing business rows, and performs no backfill. Validate upgrade both from 0037 with representative existing data and from an empty database, plus Unicode, timestamps, indexes/FKs, restart and rollback-image readiness. Keep the normal SQL readiness/isolation/privacy checks.

Keep the revision self-contained with Alembic/SQLAlchemy imports: a rollback bundle must be able to load the new migration lineage without importing feature implementation modules that are absent from the previous application.

## 3. Command and HTTP contracts

All results below describe committed state; a flush is insufficient. The application service owns a short unit of work through a repository/transaction port. Do not publish cache success before a FastAPI dependency later commits.

| Method and path | Authority | Behavior |
| --- | --- | --- |
| GET /api/v1/runtime/status | Any authenticated persisted AppUser | Observe this caller's ephemeral presence, return active broadcast and only this user's receipt |
| POST /api/v1/runtime/broadcasts/{id}/acknowledgment | Same | Acknowledge only AuthPrincipal.local_user_id, body contains no user identity |
| POST /api/v1/admin/runtime/broadcasts | admin_settings | Explicit confirmed immediate creation with command UUID, title/message |
| POST /api/v1/admin/runtime/broadcasts/{id}/end | admin_settings | Idempotent explicit end of this exact identity |
| GET /api/v1/admin/runtime/status | admin_settings | Active broadcast, present/recent users and receipt states/counts, freshness/completeness |
| GET /api/v1/admin/runtime/broadcasts | admin_settings | Cursor-paginated history, default 25/max 100 |
| GET /api/v1/admin/runtime/broadcasts/{id}/acknowledgments | admin_settings | Cursor-paginated committed receipt history, default 100/max 250 |

Explicitly classify these exact paths/methods in required_permission before generic fallbacks. Normal endpoints require authentication, not a business-module/manage permission; they must never enter the public-path set. The admin prefix requires admin_settings for GET and POST alike, before a generic GET read grant. Preserve middleware authentication and CSRF; body schemas reject extra user/actor fields.

Reuse the existing session cookies, credentialed fetch and csrfHeaders for POST. Do not widen manage_communications or give admin_settings to coordinators. Do not add a new permission for the current ADMIN-only operational surface. Revisit that decision only if delegation is later requested.

A null/missing persisted local_user_id is an explicit identity error, never a name/email/employee fallback. Production OIDC and break-glass already resolve AppUser. Local development's existing bootstrap materialization supplies it; tests must do the same. No new production provisioning path is permitted.

### Status response

Return server_time_utc, observation_epoch, presence renewal/completeness diagnostics relevant to this caller, and either active_broadcast = null or:

- id, version = 1, activated_at;
- acknowledged_at, nullable, for the authenticated caller;
- content with title/message, unless the caller's optional validated known_broadcast_id/version matches the immutable identity.

Always send the active identity and receipt state. A content-unchanged response is not an empty/no-broadcast response. A browser lacking the matching content must refetch without the hint. The hint changes payload size only; it supplies no identity, permission or receipt authority. Typical unchanged/absent responses should fit in 1 KiB excluding headers.

Use Cache-Control: no-store, private and Vary: Cookie on authenticated responses, including error responses; fetch uses cache: no-store. Nginx must not cache these endpoints. Presence observation is incidental in-memory access bookkeeping only; GET must never create a durable heartbeat, receipt, lifecycle event or business change.

### Creation/end

Creation inserts ACTIVE in its own transaction. The same actor/command with the same normalized payload returns the existing broadcast, even if it has since ended; do not reactivate it. Same key/different payload returns 409 idempotency_conflict. Different creation while ACTIVE returns 409 broadcast_already_active, including the winner's identity for authorized admins. On a unique failure roll back before classifying/re-reading the command/winner. Do not interpret arbitrary IntegrityError as active conflict.

End uses a conditional UPDATE of the named broadcast WHERE state = 'ACTIVE', in a short transaction. Only the winner writes ended_at/actor. A no-op rereads and returns its first end result. Repeating an end for old A never ends new B. Unknown IDs return 404. There is no edit, automatic replacement or scheduled activation endpoint. The creator receives the same explicit acknowledgment requirement as other users.

## 4. Acknowledgment algorithm and races

1. Authenticate and validate CSRF through the current server path; derive user_id exclusively from the principal.
2. Start a feature-only READ COMMITTED transaction and attempt one receipt INSERT. Do not pre-read/take update locks on a missing receipt or the broadcast. FKs validate existence.
3. Commit; then return HTTP 200 acknowledged with the stored canonical timestamp and publish the positive cache entry.
4. For a confirmed duplicate on the receipt primary key, fully roll back/close the failed transaction. In a fresh READ COMMITTED transaction, SELECT the exact (broadcast_id, authenticated user_id). Return HTTP 200 already_acknowledged with that unchanged timestamp.
5. A confirmed missing broadcast/FK target returns a safe 404 or identity error as appropriate. Other DB errors retain their own classification. A duplicate handler that cannot find a canonical row must not manufacture success.

Do not depend on savepoint recovery, the connection's XACT_ABORT setting, IGNORE_DUP_KEY, MERGE, a broad serializable transaction, or sp_getapplock on the broadcast. Match SQL Server native duplicate codes 2601/2627 and the expected constraint/key; inspect canonical state after rollback. Commit uncertainty is resolved by reading/retrying the same natural key.

For transient deadlock victim 1205, roll back and allow at most two retries with 25–100 ms jitter, within the request budget; count every raw victim. Persistent lock/connectivity/timeout failure is a safe retryable 503, never a false receipt or unhandled 500. No sleep occurs while holding a transaction/connection. Tests must record zero raw deadlocks in the expected workload; automatic retries do not turn a failing load gate green.

Accept receipts for retained ENDED broadcasts as well as ACTIVE ones. This solves an end-versus-click race without a per-click ACTIVE row update/lock. A late receipt preserves its actual time, is labeled late in admin history when after ended_at, and cannot claim the user saw a warning before its end. Once retention removes the broadcast, late requests receive 404 and the next successful status resolves any stale modal.

### Required cases

| Case | Canonical result |
| --- | --- |
| Mary's three tabs insert together | One receipt; all return 200; all see the exact same stored timestamp |
| First transaction rolls back | A waiting contender may become the first committed insert; failed attempt leaves no receipt/audit |
| Server commits but HTTP response is lost | Retry returns the same committed receipt/time |
| One account on two PCs | Same AppUser and receipt; first success wins across devices |
| Offline tab wakes after another PC acknowledged | Successful status suppresses its modal from server receipt state |
| Old A acknowledgment returns after new B appears | Record A success locally; do not dismiss or mark B acknowledged |
| End A races with acknowledgment A | Receipt may be late, but still succeeds while A is retained |
| Two admins create different broadcasts | SQL filtered uniqueness admits one ACTIVE; loser gets defined 409 |
| Two ends or retried creation | First lifecycle actor/time preserved; no duplicate lifecycle record |

Do not mutate a shared acknowledged_count. Derive counts with indexed receipt queries in the lower-frequency administrative projection. ACK writes may take compatible FK/key/page locks; no architecture claim eliminates all SQL physical contention. Observe it with real load.

## 5. Presence and administration semantics

The in-memory key is AppUser.id. Record last_seen_monotonic, last_seen_utc and bounded server-resolved display name. No browser/tab/session ID, keypress tracking, unload beacon, raw session token, IP address or durable heartbeat is necessary. Each successful authenticated status observation renews the same record, so user presence is the union across all its documents/devices.

Active means now_monotonic - last_seen_monotonic <= 60 seconds. Sleeping/lost-network clients naturally expire; browser-close notification is not required. This means recently observed application access, not proof someone is watching, has unsaved work, or can safely be disconnected.

Retain recent observations for 15 minutes, prune at least every 30 seconds and on capacity pressure. Cap at 10,000 users and bound field lengths; target combined feature cache/presence memory <= 16 MiB, measured in load tests. At cap, remove expired/inactive entries first. If all entries are active, do not silently evict one and report a complete roster: mark presence incomplete, decline tracking the additional entry, and keep delivering its broadcast. Keep the capacity-incomplete flag for at least 60 seconds after the latest overflow.

On startup generate an observation_epoch, clear only ephemeral data, and mark presence warming for 60 seconds. Administrative results include generated_at, epoch, warm-up/completeness and stale/error flags. A backend restart or capacity overflow must not look like proof that no user remains pending.

The admin surface shows one person per user. Join recent presence with current-broadcast receipts in a bounded set-oriented query; batch user identity lookups, no per-user SELECT loop. Include:

- active acknowledged and active pending users;
- last_seen_utc and canonical acknowledged_at;
- inactive/recent users separately labeled;
- durable receipts with no current observation as inactive/unknown, with last_seen absent.

active_pending_count = currently observed active users without a receipt. Do not count departed users as currently active pending, or historical receipts as currently active. Counts are a generated-time operational projection, not an atomic snapshot of the moving world. Paginate display; retain exact counts for the complete tracked set. Never expose other users' presence or receipts in the normal status response.

History preserves receipts, not historical active-user snapshots or a list of everyone who missed a broadcast. No durable evidence of historical nonacknowledgment is claimed. Admin polling runs only while that screen is mounted, at 10 seconds with ±2-second jitter and explicit refresh after its own lifecycle actions. It is not a second heartbeat; the one global status loop remains responsible for presence/delivery. Show time since last successful refresh and retain stale rows on failure.

Multiple Uvicorn workers/replicas would split the roster and local receipt accelerators. Before enabling them, choose shared ephemeral presence, common completeness semantics and bounded cross-process notification/cache rules in a new gate. Sticky sessions alone are insufficient. Bounded active-state TTL already detects outside commits; it does not solve a split roster.

## 6. Cache and admission contracts

Cache the active immutable DTO or absence for <= 1 second, using monotonic time and one in-flight loader per process. Ordinary concurrent misses await that loader without holding a memory lock over SQL. Never launch one SQL refresh per polling client.

After create/end commit, atomically increment a local generation and invalidate/publish current state. An older loader may not republish its pre-commit result. A refresh observes the generation before and after its SQL read and discards obsolete work. Outside-process commits are discovered at expiry. Do not cache a SQL error as absence; after expiry report 503 rather than presenting stale state as fresh.

Positive receipts may be cached by (broadcast_id, user_id), maximum 10,000 entries with bounded eviction. Publish only after commit or a committed SQL read. Cache misses do a single indexed lookup when a broadcast is active; do not cache negative results. Same-process successful acknowledgment is visible immediately to later polls; cross-process receipts appear on the next SQL lookup. Eviction costs a read, never changes truth. Ended-broadcast entries can be discarded.

The current shared SQLAlchemy QueuePool has no overrides: default 5 pooled connections plus up to 10 overflow connections. This is a 15-connection engine checkout ceiling, not 15 per user; underlying pyodbc pooling is a separate consideration. Verify actual configured versions and pool state during implementation.

For this feature, bound admission to 6 concurrent HTTP operations including their authentication/CSRF and repository work, with a maximum queue of 512 and <= 3-second admission wait. Apply admission before feature authentication starts SQL, including admin runtime routes. Wait asynchronously, without a DB connection or worker thread. Normal business routes do not consume this feature gate. Feature work must not create another engine or enlarge the common pool automatically.

Release the admission permit only after all synchronous DB work for that operation has actually stopped. Browser AbortController or a canceled coroutine does not prove a pyodbc statement stopped. Use driver/checkout/lock timeouts and short units of work; restore scoped connection settings before returning pooled connections. Admission exhaustion returns a safe 503 with Retry-After and jittered retry. The mandatory target workload must succeed without admission rejections; queue limits are overload protection beyond it.

Offload synchronous resolver and CSRF execution through the existing sync/awaitable protocol using a context-propagating threadpool helper; preserve support for asynchronous resolvers. Every Session is created/used/closed inside its synchronous call, never shared with the event loop or another worker. This is a bounded prerequisite for the new polling volume, not an authorization cache, identity-policy rewrite or ERP scheduler change. Add an event-loop responsiveness/slow-auth regression check and the existing authentication/CSRF regressions.

## 7. Browser scheduling and global blocking contract

Implement one provider in main.tsx within the identity-keyed lifetime and above ViewScopeProvider/App route branching. The provider needs only auth and its runtime API, so ViewScope/business failures do not disable warnings. Start only with a resolved authenticated principal and stop on logout/unmount/identity change. Cover detached Planning windows explicitly.

Use recursive setTimeout scheduled after completion with independent uniform 4–6-second delays, never setInterval plus overlapping fetches. First check is immediate. Hidden documents retain the same desired cadence; browsers may throttle/freeze them. On visibility return, focus, online or pageshow, coalesce events and schedule one check within 0–1 second, canceling the old timer. Do not run catch-up polling for missed intervals.

Status requests have a 3-second client deadline. ACK requests have a 10-second deadline, disable repeated clicks only within that tab, and are not automatically retried in a tight loop. Network/5xx status failures back off at approximately 10, 20, 40, then at most 60 seconds with ±20% jitter capped at 60. A resume/online event can expedite one check. Resume normal cadence on success. After ACK success, schedule the next status check with 4–6-second jitter, not an immediate synchronized fleet refresh.

Every effect owns and cancels its timer, event listeners and AbortController. Check the current identity generation before applying any response. React StrictMode's mount/cleanup/remount must leave one loop. Route changes create no loop. A poll started before ACK success cannot reopen that identity's modal: retain the server-confirmed receipt locally and reject older pending state. A successful ACK for A never changes B's state. No BroadcastChannel or localStorage receipt is required or accepted as server truth.

Use a top-layer accessible dialog with role alertdialog, aria-modal, labeled heading/message/time, initial focus on the large J'ai compris action, trapped keyboard focus and inert underlying interaction. Preserve/restore the previous focus when appropriate. Prevent Escape and backdrop dismissal. Ensure existing native/custom business dialogs cannot appear above it or continue their focus trap; preserve their draft state rather than closing/recreating the page.

Show multiline plain text via React text nodes. No dangerouslySetInnerHTML, link autoload, Markdown parser or script execution. The displayed activation time comes from the server and may be localized using the existing locale.

Keep the modal open with a clear retry/error state until ACK succeeds. A transient failure never records local acknowledgment. A successful status that shows no active warning, or new B replacing ended A, resolves the old modal; B requires its own server receipt. Unknown/error/aborted responses do not mean ended.

A poll failure, including 401/403 during expiry/permission/CSRF changes, must not directly reset AuthProvider, navigate away or discard business forms. Show a non-destructive reconnect/sign-in indication where needed; existing explicit authentication flows retain authority. The server continues to deny unauthorized API operations. Do not turn SQL errors into 401. If an old frontend remains open after deployment, it has no broadcast provider: require a reload/controlled rollout and do not promise notification to code that predates the feature.

## 8. Failure, restart, maintenance and rollback

| Event | Expected behavior |
| --- | --- |
| Poll/proxy/backend failure before a message was received | Existing app remains mounted; retry/backoff; no invented message or forced logout |
| Failure while unacknowledged modal is open | Keep the known message blocking; ACK shows failure/retry; no offline success |
| Backend/Docker restart | Broadcast/receipts persist; caches reload; presence enters a new warm-up epoch; clients reconnect |
| Temporary SQL loss | Existing auth also depends on SQL: safe 503, no bypass using cached principal; no fresh empty state or receipt success |
| SQL loss on admin screen | Keep stale rows visibly stale; disable trusted creation/end success; show inability to confirm state |
| Response lost after commit | Retry creation command/receipt/end returns canonical committed result |
| Broadcast ends while a modal is open | Next successful status dismisses that identity; old in-flight receipt can still succeed |
| Network/browser wakes after suspension | One coalesced check; receipt and current identity resolved from server |

Map feature-path database failures from authorization as well as route/service code to safe retryable 503 responses. No DBAPI details, credentials or raw connection strings enter the response/log.

Normal healthy, runnable foreground clients target <= 7 seconds from committed creation to visible modal (4–6-second next timer plus processing/cache allowance). This is not a guarantee for hidden/throttled/suspended browsers, disconnected PCs, SQL outage or a frontend loaded before the feature. On resumption perform the immediate check. The admin observes last_seen/pending/freshness; no automatic safe-to-deploy conclusion, forced logout, API maintenance lock or server shutdown is introduced.

Rollback compatibility has two separate requirements:

1. The additive tables leave previous business code/data compatible; never delete them or run automatic Alembic downgrade.
2. Current check_database_readiness requires exact revision equality. The unmodified PREV image expects 0037 and therefore reports not_ready against the new head. Additivity does not solve that.

Before promotion, build and verify a separately tagged rollback bundle preserving PREV application behavior while carrying the accepted forward migration lineage metadata needed for exact readiness. It must not execute a downgrade or alter business behavior; preserve image provenance and record the tag in the runbook. Test /health, /ready, auth and ordinary read/save flows against the upgraded database, plus the normal drain/start procedure. Do not silently substitute the ordinary PREV tag or relax readiness to accept arbitrary future schemas. If no tested rollback bundle exists, hold feature promotion.

When rolling back to code without this feature, tables/receipts survive, but warning delivery, ACK endpoint, cleanup and UI are unavailable until compatible code returns. Old loaded clients may show retained warnings/error state. Prepare an operator communication/reload plan; the feature cannot guarantee warnings while its supporting backend is intentionally removed.

Keep the ADR-033 drain → migrate → start → readiness sequence, scheduler configuration, lease/fencing and existing rollback constraints intact.

## 9. Estimated load and scalability

These are planning estimates, not measurements or a production-capacity certification. C is concurrently polling tabs/documents, including multiple tabs per person. Rates conservatively use C/5; completion-based timing reduces them slightly.

| C | Poll requests/s | Up to auth SELECTs/s (3 per poll) | Total SELECTs/s, no broadcast | Total SELECTs/s, all pending |
| ---: | ---: | ---: | ---: | ---: |
| 20 | 4 | 12 | 13 | 17 |
| 50 | 10 | 30 | 31 | 41 |
| 100 | 20 | 60 | 61 | 81 |
| 250 | 50 | 150 | 151 | 201 |
| 500 | 100 | 300 | 301 | 401 |

Totals include at most approximately one cache refresh SELECT per second per serving process. The last column adds one indexed receipt read per pending poll. Positive cache hits remove that extra read after acknowledgment. Exclude admin history, writes, ordinary APIs, expired cache eviction and one-time initialization. A GET status adds no CSRF SELECT; command POSTs still incur CSRF checks.

At 20–100 clients, expected incremental CPU/memory/network work is small, but SQL auth round trips still dominate. At 250–500, 50–100 requests/s and 150–300 auth reads/s must be tested against real VM/SQL latency. Moving blocking work off the event loop and retaining bounded pool/admission behavior are essential. Do not assert that 500 clients is validated from this arithmetic.

At <= 1 KiB steady response body, body traffic is approximately 4/10/20/50/100 KiB/s respectively, plus request/response headers and transport overhead. Initial full-message delivery is a bounded one-time larger response, up to roughly 16 KiB plus metadata; unchanged-content hints avoid resending it every five seconds.

Presence and positive receipts are bounded O(users), not O(tabs/history). Admission/cache locks are short and never held over SQL. No idle socket per client or SQL connection per browser is retained. Normal SQL sessions close before client wait/backoff; the shared engine retains its configured pool.

Jitter reduces synchronized steady polling. Initial login, reconnect and ACK waves still require explicit burst testing. A 250-request ACK wave queues through bounded feature admission as independent small transactions; it does not open 250 SQL connections or update one common count row.

## 10. Required validation and measurable delivery gates

Use disposable, isolated test databases and synthetic accounts. Never execute the load tool on production. SQLite fast tests are useful for behavior only; live SQL Server is mandatory for engine-sensitive results.

### Functional, authorization and browser

- Admin creates/confirms; all current modules, technician-only account, detached Planning and an already open real-backend browser receive automatically.
- Late opener sees active content; ACK permits continued editing/saving with the draft intact; a new ID appears after prior ACK.
- Non-admin cannot create/end/read another user's roster/history; normal caller cannot supply identity; unauthorized/CSRF failures occur before writes.
- Plain-text HTML/script-like payloads, accents and astral Unicode remain literal; length boundaries agree with NVARCHAR.
- Keyboard/screen reader labels, tab trapping, focus restore, Escape/backdrop refusal and pre-existing dialogs meet the global contract.
- Navigation, StrictMode, identity switch and late-response/ACK-versus-poll races leave one loop and no prior-user data.
- Offline/server/SQL/401/404/version-mismatch behavior, wake, two PCs/three tabs, ended/new broadcast and restart preserve the stated invariants.
- Confirm that Planning/Shift/Demand/ERP rows and global mutation version remain unchanged.

### Live SQL concurrency

Use independent connections/transactions and a start barrier, not one shared Session or serial requests labeled concurrent. Record actual server overlap/queue timing.

- Two different admin commands race to create ACTIVE: exactly one active row, deterministic loser 409. Repeat same command/payload and changed-payload retries.
- Two end commands race, plus end/create and old-ID retries: stable first actor/time, no end of new broadcast.
- Mary's three real authenticated requests race: one receipt, three 200s, identical canonical timestamp. Repeat with lost response and a first writer deliberately rolled back.
- Run both 100 and 250 simultaneous authenticated ACK requests; include distinct users and >= 25% duplicate requests distributed across overlapping keys. Also run 250 distinct users plus 63 additional duplicate requests.
- After each run, compare receipt counts with distinct user keys; verify immutable timestamps across retries, no duplicate audit/events, no shared-row counter update, no leaked transaction/connection and no business locks/changes.
- Run at least 20 repeated waves. Observe raw error 1205/deadlock events, lock waits and index/FK contention; zero deadlocks, zero HTTP 500s and all target ACK requests return functional 200. Queue protection must not reject this workload. Capture sanitized deadlock graphs if any; fix before acceptance.
- Exercise current SQL Server isolation settings without silently changing the database to RCSI/SNAPSHOT. Record settings and version. Include duplicate/error recovery with XACT_ABORT ON/OFF where supported; all failed transactions are fully rolled back.
- Interleave bounded cleanup on old ended broadcasts; active/current acknowledgments stay isolated and history remains referentially sound.

### Mixed HTTP load

Run the actual Nginx → one Uvicorn process → real SQL Server stack, production-like VM limits and realistic simulated SQL-backed session cookies/CSRF. Static-auth unit tests do not measure auth cost. Keep access/performance logging configured as intended for production.

Use repeatable baseline and feature-on runs on the same seeded data:

1. Warm up for two minutes, then measure at least ten minutes each at 20/50/100/250/500 polling documents.
2. Measure no-active, all-pending and mostly-acknowledged states. Keep a foreground ordinary-API workload, at least 10 requests/s, including representative Planning reads and safe synthetic create/edit/save operations in the isolated database.
3. During that same workload inject 100/250 ACK waves, duplicate-key waves and coordinated reconnect/wake waves.
4. Verify ordinary API p95/p99 against baseline, server CPU/RSS/event-loop lag, auth/DB timing, query counts, pool checkout/wait/overflow, connection totals, network, log volume and browser delivery latency.

Acceptance thresholds for this workload:

| Measure | Required result |
| --- | --- |
| Ordinary API p95 and p99 increase | <= max(10% of baseline, 50 ms), and existing route budgets still pass |
| Status endpoint, healthy steady state | p95 <= 250 ms; p99 <= 750 ms |
| ACK burst | p95 <= 2 s; p99 <= 5 s; all target requests 200, zero 500/deadlock/admission rejection |
| Foreground delivery | p95 <= 7 s after committed activation |
| Single-process event-loop lag | p99 <= 100 ms under mixed load |
| SQL | Feature adds zero durable heartbeat writes; shared checkout ceiling respected; no persistent pool starvation |
| Memory | Feature caches/presence <= 16 MiB at caps; no growth after expiry/repeated waves |
| CPU | No sustained saturation; target <= 70% allocated CPU averaged over one minute, with ordinary API budgets preserved |
| Regression suites | Existing V2 performance budgets, auth/CSRF, boundaries, migration and real-backend E2E pass |

A threshold failure blocks promotion, not permission to weaken the threshold, omit authentication, increase SQL connections/workers silently or label 500 users supported. Return measured evidence to PO; escalate only if a different architecture is needed.

Run the existing benchmark_v2_api.py --iterations 3 --ci, isolation/readiness checks, targeted/neighboring tests and normal CI. Add the minimum live SQL job trigger/module wiring for #894 in Phase 1 so its migration/concurrency proof is executable, and extend it for the final Phase 4 load gate. An equally explicit dedicated job is acceptable. A skipped SQL job is not acceptance. Attach a concise machine-readable load report with exact head, environment, data cardinality, SQL settings, seed, durations, latency/error distributions and SQL counts. Do not commit credentials or user data.

## 11. Retention and maintenance

Retain ended broadcasts and their receipts for 90 days from ended_at; active broadcasts never expire automatically. Presence remains purely ephemeral with its separate 15-minute horizon. Identity/lifecycle rows contain only necessary user IDs and bounded message data; names in admin history are current account labels, not a historical identity snapshot.

Add one feature-owned lifespan task: prune memory every 30 seconds and attempt SQL retention hourly with jitter, first SQL cleanup no earlier than one hour after startup. Keep it independent of ERP-enabled state and occurrence/scheduler control. Cancel/drain it cleanly at shutdown.

Delete only receipts for expired ended parents, in <= 1,000-row transactions, then <= 100 empty expired parents per transaction, with <= 5 seconds of work per hourly pass. The receipt key and ended-history index bound selection. No unbounded cascading delete, startup backlog scan or business cleanup is allowed.

Recheck parent eligibility before deletion. If a very late receipt races expiry, preserve integrity, roll back/retry a conflicting parent deletion in a later bounded pass; do not turn an expected retention race into a user 500. Never delete active data. Expose backlog/oldest eligible age and alert operationally if bounded cleanup cannot keep up. Ninety days is the retention target; an outage/backlog may delay deletion and must be visible. This is not an indefinite audit/event store.

## 12. Observability

Reuse existing technical timing phases/route templates. Expose bounded counters or aggregate diagnostic records for:

- active broadcast ID/activation time; observation epoch/warm-up and tracked active-user count;
- presence capacity/incomplete flag; generated-time active pending/acknowledged counts;
- poll counts, p95/p99, errors/backoff/overload, cache hits/refresh failures;
- ACK inserts, idempotent duplicates, DB failures, raw deadlocks/retries and latency;
- feature admission queue/in-flight count; pool waits and cleanup deleted rows/backlog/error.

Successful high-frequency status requests must not add a per-heartbeat log stream. Apply route-specific sampling (one in 20 successful polls) to existing performance output and suppress/sample matching success access logs. Preserve aggregate counts and all ordinary-route behavior. Aggregate repeated poll failures by safe code at most once per minute, while counting every occurrence. Lifecycle commands have one structured committed outcome; duplicate receipts increment a technical counter, not a new durable audit.

Retain existing bounded JSONL rotation; do not introduce Prometheus/Redis/OpenTelemetry dependencies solely for this feature. Use route templates, broadcast IDs and safe error codes, never cookies, token/hash material, CSRF values, raw SQL/parameters, full messages, employee IDs, display names or email in diagnostics. Full admin content remains in authorized SQL/UI only.

## 13. DEV execution and production handoff

Recommend one coherent #894 implementation WorkItem and one Draft PR, executed through four bounded phases under AGENTS.md §3B. No new sub-issues are needed.

At gate review, canonical machine roadmap #851 contains ASTRA-894 as an ARCHITECTURE_GATE but no 894 WORK entry; its next existing WORK is blocked #860. Before DEV can start, PO must register the proposed 894 WORK after ASTRA-894 and before #860 through the required structural roadmap Safe Writeback preview/confirmation path, and separately authorize Phase 1. This is a concrete roadmap proposal, not authorization to mutate #851 during this gate. Acceptance must not accidentally advance #860 while the feature implementation remains unrepresented. Keep the existing issue #894 as the implementation parent; do not invent replacement identity or treat an ordinary post-merge reconciliation as structural authorization.

| Phase | Authorized objective when PO starts it | Required evidence before next phase |
| --- | --- | --- |
| 1 — persistence and application | Additive migration/models, immutable lifecycle, unique active/receipt keys, unit-of-work/idempotency, bounded retention repository and minimum live-SQL CI hook | Migration/behavior tests and live SQL create/end/ACK race proof; exact head; no frontend/background task yet |
| 2 — runtime and HTTP | Composition/routes, RBAC/CSRF, auth offload, bounded admission, status/presence/caches, safe errors, lifecycle cleanup and sampled diagnostics | Auth/boundary/cache/time/admission/failure tests, SQL request concurrency, loop responsiveness; exact head |
| 3 — global React/admin | Provider/modal, polling lifecycle, admin form/status/history, detached path and accessibility | TypeScript/build and real-backend browser cases including multiple tabs/users and failures; exact head |
| 4 — integrated validation and operations | Final load/CI wiring, isolated load/concurrency tooling/results, restart/promotion/rollback runbook and tested rollback bundle | Complete exact-head CI including non-skipped live SQL, mixed-load report, rollback readiness proof, Architect conformance and PO PMRA |

Each phase is a checkpoint on the same branch/PR, not a release. Only the current phase is authorized; report deferred scope and stop. Do not merge partial phases. No production behavior is implemented or DEV dispatched by this architecture gate.

For the implementation's merge, Architect conformance review is REQUIRED for access control, SQL concurrency, new lifecycle background work and rollback obligations. PO must inspect exact-head CI/results, complete PMRA, mark Ready only with merge authorization, and leave merge to the human operator. Normal PREV rollback alone is insufficient until the exact-head/schema-compatible rollback proof exists.

The architecture has no remaining product ambiguity requiring a decision. The selected defaults (one immutable active broadcast, user-level ACK, 60-second presence, 90-day history, admin_settings) are the gate's decisions. Residual delivery risks are empirical capacity on the target VM/SQL, suspended/old browsers, single-process presence and rollback-bundle readiness; their acceptance requirements above must remain visible at handoff.

## External technical references

- [Microsoft: filtered unique indexes](https://learn.microsoft.com/en-us/sql/relational-databases/indexes/create-filtered-indexes).
- [Microsoft: CREATE INDEX / connection SET requirements](https://learn.microsoft.com/en-us/sql/t-sql/statements/create-index-transact-sql).
- [Microsoft: deadlocks and error 1205](https://learn.microsoft.com/en-us/sql/relational-databases/sql-server-deadlocks-guide).
- [Microsoft: XACT_ABORT](https://learn.microsoft.com/en-us/sql/t-sql/statements/set-xact-abort-transact-sql).
- [SQLAlchemy: default QueuePool limits](https://docs.sqlalchemy.org/en/20/core/pooling.html).
- [SQLAlchemy: SQL Server/pyodbc types and pooling](https://docs.sqlalchemy.org/en/20/dialects/mssql.html).
- [FastAPI: concurrency and sync execution](https://fastapi.tiangolo.com/async/).
- [MDN: visibility/background timer limits](https://developer.mozilla.org/en-US/docs/Web/API/Page_Visibility_API).
