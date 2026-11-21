# Issue #894 — Phase 4 integrated validation and rollback runbook

This runbook is the reproducible Phase 4 evidence contract for ADR-038 / ASTRA-894.
It does **not** authorize a production deployment. PR #897 remains Draft until the
Product Owner completes the downstream Architect review and Production Merge Risk
Assessment.

## Safety boundary

The load driver refuses to run unless both conditions are true:

- RESOURCEPLANNER_PHASE4_ISOLATED_LOAD=ISSUE_894_DISPOSABLE_SQLSERVER;
- the SQLAlchemy URL targets a database whose name contains
  ResourcePlanner894Load.

All accounts, sessions, projects, Planning rows and broadcasts used by the load
job are synthetic. Credentials are written to a temporary runner directory and
are never uploaded as CI evidence.

## Certification stack

The dedicated CI job starts:

~~~
Nginx frontend
  -> exactly one backend container
     -> exactly one Uvicorn process
        -> disposable SQL Server 2022 Developer
~~~

The backend is started through tools/issue_894_phase4_server.py, which wraps
the normal configured application and adds only a test-only event-loop-lag
sampler. It does not configure Uvicorn workers or replicas.

The repository-controlled production target currently has the same topology:
docker-compose.yml declares one backend service, Dockerfile.backend runs
python -m app.server, and app.server.runtime.run_server() invokes one
uvicorn.run() without a workers value. Multiple production workers/replicas
remain outside ADR-038 and require a new architecture gate.

The physical production VM is not observable from GitHub Actions. Before actual
promotion, the operator must record these commands from the Ubuntu VM and
confirm exactly one serving backend container/process:

~~~bash
docker compose ps
docker compose top backend
docker inspect "$(docker compose ps -q backend)"   --format '{{.Name}} {{.Config.Image}} {{.State.Status}}'
~~~

Any second serving backend process/replica is a **promotion blocker** for #894;
do not change the architecture or rely on sticky sessions to hide a mismatch.

## Mixed-load matrix

tools/issue_894_phase4_load.py run executes, on one seeded SQL database:

- one ordinary-API baseline;
- 20 / 50 / 100 / 250 / 500 polling documents;
- no_active, pending, and mostly_acknowledged states at every level;
- two-minute warm-up and ten-minute measurement per scenario by default;
- an ordinary foreground workload at 10 requests/s;
- Planning snapshot reads, project reads, and safe synthetic resource
  create/edit/save operations;
- completion-based 4–6 second polling through Nginx;
- SQL-backed session-cookie authentication;
- event-loop-lag sampling.

The report fails when the accepted thresholds are exceeded:

- ordinary API p95/p99 increase greater than max(10%, 50 ms);
- status p95 > 250 ms or p99 > 750 ms;
- foreground delivery p95 > 7 s;
- event-loop lag p99 > 100 ms;
- non-200 status responses or SQL deadlocks.

Docker statistics are sampled independently. The CI summarizer treats backend
CPU above 70% of allocated runner CPUs, averaged over a rolling 60-second
window, as a failed Phase 4 gate. The bounded in-process cache/presence
16-MiB contract has a separate deterministic regression test.

## ACK-wave proof

The same job executes at least 20 repetitions of each shape:

- 75 distinct users + 25 duplicate requests = 100 concurrent HTTP ACKs;
- 187 distinct users + 63 duplicate requests = 250 concurrent HTTP ACKs;
- 250 distinct users + 63 duplicate requests.

All requests cross Nginx and the normal FastAPI authentication/CSRF/admission
boundary. A raw HTTP request is intentionally disconnected before its response
is consumed, followed by a normal retry, to exercise lost-response idempotency.
The report checks receipt cardinality, stable canonical timestamps, response
codes, p95/p99, and SQL deadlock deltas.

## Migration, restart, authentication and ordinary-flow proof

The CI job proves both migration paths:

1. empty disposable database -> alembic upgrade head;
2. separate database at 0037_workforce_request_project_manager_phone with
   representative Project, Demand and Shift rows -> alembic upgrade head,
   followed by preservation checks.

The feature stack then proves an actual backend-container restart:

- active broadcast persists;
- canonical user acknowledgment persists;
- the ephemeral observation epoch changes.

Normal CI remains authoritative for OIDC/auth/CSRF, RBAC, browser, migration,
server-isolation and real-backend acceptance. The Phase 4 load stack adds
SQL-backed session cookies and CSRF to the measured HTTP paths rather than
using static principals.

## Deployment/promotion validation

The supported sequence remains:

1. identify immutable backend/frontend image SHA and current production tag;
2. drain/stop application writes according to the existing operations runbook;
3. take/verify the SQL Server backup required by operations;
4. run the explicit forward migration (alembic upgrade head);
5. start the backend and frontend without rebuilding promoted images;
6. require /health and /ready 200;
7. verify authentication and representative ordinary read/save flows;
8. only then reopen traffic after the human go/no-go decision.

The repository does not version the VM-specific build.sh, deploy-dev.sh or
promote-prod.sh. Phase 4 therefore validates the repository Compose/image
contract and produces commands/evidence for the operator; it does not claim
those private host scripts were executed.

## Schema-compatible rollback bundle

tools/build_issue_894_rollback_bundle.sh creates a separately tagged bundle
from:

- the PREV application commit (the PR base SHA);
- **only** the accepted 0038_emergency_broadcasts.py migration lineage from
  the feature head.

The generated tag is:

~~~
rollback-894-<PREV-12>-schema0038-<FEATURE-12>
~~~

Both images carry the PREV full SHA in
org.opencontainers.image.revision. The manifest separately records the
feature SHA that supplied the migration lineage and its SHA-256. The archive
contains the exact tested backend/frontend images.

The rollback proof runs those PREV-behavior images against the database that is
already at migration 0038, then requires:

- /health;
- /ready with exact 0038 head;
- existing SQL-backed authentication;
- ordinary Planning read;
- ordinary resource create/edit/save.

No Alembic downgrade is executed. The 0038 tables and retained broadcast/receipt
data remain in place. Readiness is not weakened to accept unknown future
schemas.

If the rollback bundle build, provenance check, exact-head readiness, or
ordinary-flow smoke fails, **promotion is blocked**.

## CI evidence

The Issue #894 Phase 4 load / rollback job uploads an artifact named
issue-894-phase4-evidence-<head SHA>. It intentionally excludes the synthetic
session credential file. Expected files include:

- mixed-load/ACK JSON report;
- SQL predecessor migration report;
- restart proof;
- Docker CPU/memory samples and summary;
- V2 API benchmark JSON;
- rollback manifest, tag and image archive;
- rollback smoke report;
- bounded technical log-volume evidence.

Do not interpret a missing/skipped artifact or a skipped live SQL job as Phase 4
acceptance.
