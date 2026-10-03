# AGENTS.md

This file defines the working rules for coding agents operating in the RessourcePlanner repository.

Its purpose is to let an agent complete approved development work autonomously while preserving the application's architecture, business rules, tests, CI reliability, and GitHub roadmap.

---

## 1. Source of truth

For implementation work, use the following sources in this order:

1. current code and tests on `main`;
2. the GitHub Issue defining the requested work;
3. related roadmap / parent GitHub Issues and their checklists;
4. architecture and technical documentation under `docs/`;
5. existing PR discussions when directly relevant.

Do not treat previous chat conversations as more authoritative than the repository.

Do not redo an analysis that is already documented unless the relevant code or requirements have materially changed.

When starting work, always synchronize with the current `main`.

---

## 2. Main architecture

The target Web application is:

```text
React
  ↓
FastAPI
  ↓
Application / Domain
  ↓
Infrastructure
  ↓
SQLAlchemy / database / external integrations
```

Relevant source areas include:

```text
frontend/                    React / TypeScript / Vite
app/server/                  FastAPI HTTP layer and composition
app/application/             application services and use cases
app/domain/                  business/domain rules
app/infrastructure/sql/      SQLAlchemy persistence
app/infrastructure/acumatica/
app/infrastructure/m365/
app/infrastructure/smtp/
migrations/                  Alembic migrations
tests/                       Python verification suite
tools/                       validation, benchmark and maintenance tools
```

Preserve these boundaries unless an approved architectural change explicitly requires otherwise.

### Web V2 authority rules

The following rules are architectural constraints:

- FastAPI is the mutation boundary for the React application.
- Python remains authoritative for planning and capacity business rules.
- React must not duplicate authoritative planning calculations or non-double-counting rules.
- The browser must not call Acumatica directly.
- The canonical Web/SQL server runtime must remain independent from NiceGUI and Excel dependencies.

Do not reintroduce legacy V1 dependencies into the canonical Web/SQL runtime.

---

## 3. Normal development lifecycle

When asked to implement an approved issue or sub-issue, continue autonomously through:

```text
understand scope
→ inspect relevant code
→ implement
→ targeted tests
→ broader relevant validation
→ create/update PR
→ enable auto-merge when the PR is complete and eligible
→ CI
→ diagnose failures when needed
→ correct failures
→ CI green
→ automatic merge
→ update relevant GitHub roadmap issue
→ next approved READY sub-item
```

Do not stop simply because:

- implementation is complete;
- a PR has been created;
- CI has started;
- CI failed for a normal implementation reason.

For normal development work, the expected endpoint is a merged, validated change.

---

## 3A. Long-running and verbose commands

Interactive agent sessions should avoid unnecessary terminal output and avoid making a long local command the only place where validation state exists.

For commands that can produce large logs or run for a long time:

- prefer `-q`, `--quiet`, or an equivalent reduced-output mode when it preserves useful warnings, errors, and a reliable exit status;
- do not use `--silent` blindly when it would hide diagnostics needed to understand a failure;
- avoid `--verbose` unless diagnosing a specific failure;
- when practical, redirect full output to a temporary log and print only a concise summary on success;
- on failure, inspect or print only the relevant tail/section first, then rerun the failing command with greater verbosity only if needed;
- run the smallest targeted tests first instead of repeatedly running the full suite locally;
- use GitHub Actions for complete repository validation when the equivalent local command is unusually long or noisy;
- never treat reduced output as reduced validation: exit codes and required checks remain authoritative.

For work that may span a long validation cycle:

- keep the implementation on a named branch;
- create coherent commits/checkpoints before unusually long external validation when the current change is in a valid intermediate state;
- create or update the PR early enough that GitHub contains a recoverable record of the work;
- do not create meaningless checkpoint commits solely to satisfy this rule;
- if an interactive session is interrupted, resume from the branch/PR and observable CI state rather than restarting the implementation from memory.

A quiet command that fails should become more verbose only for the failing scope. Do not flood the conversation with successful logs that add no diagnostic value.

---

## 4. Scope discipline

Implement the smallest coherent change that satisfies the GitHub Issue.

Prefer incremental changes over broad refactors.

Do not:

- redesign unrelated components;
- clean up unrelated code merely because it was noticed;
- expand an issue into adjacent roadmap work;
- introduce a new abstraction without a concrete need;
- change business semantics only to simplify implementation.

If neighboring cleanup is useful but unnecessary, leave it for a separate issue.

---

## 5. Existing business concepts

Do not collapse distinct business concepts because they contain similar fields.

Important concepts currently include, among others:

- Project
- WorkforceRequest
- RequestLine
- WorkPackage
- ResourceRequirement
- Shift
- Resource
- AppUser / authentication identity
- operational responsibility / contacts
- ERP project and task information

Identity, business responsibility, requested resource, planned requirement, and actual shift assignment are not automatically equivalent concepts.

When changing relationships between these concepts, inspect existing services, projections, API contracts, migrations and tests before modifying the model.

---

## 6. Backend rules

For backend changes:

- keep business rules in the appropriate application/domain layer;
- avoid moving authoritative business logic into HTTP routes;
- avoid duplicating the same rule in several endpoints;
- prefer stable IDs over display names for relationships and commands;
- preserve existing API contracts unless the issue explicitly changes them;
- update serialization/API schemas consistently with model changes;
- verify authorization when adding or changing mutations.

When planning behavior changes, inspect effects on relevant:

- requirements;
- shifts;
- assignments;
- locked/manual decisions;
- capacity;
- projections;
- approvals;
- diagnostics;
- historical/audit behavior.

---

## 7. Frontend rules

The active Web frontend is React + TypeScript + Vite under `frontend/`.

For frontend changes:

- reuse the existing API instead of recreating backend rules;
- use backend IDs as identifiers rather than display names;
- preserve loading, empty and error behavior;
- keep forms efficient for their intended user;
- avoid duplicating server state unnecessarily;
- do not redesign unrelated screens as part of a focused issue.

For local React development:

```bash
cd frontend
npm run dev
```

Production validation requires:

```bash
cd frontend
npm run build
```

`npm run build` performs TypeScript validation before the Vite build.

---

## 8. Python environment

CI uses Python 3.12.

The complete compatibility dependency profile is:

```bash
python -m pip install -r requirements.txt
```

The canonical Web/SQL server profile is intentionally smaller:

```bash
python -m pip install -r requirements-server.txt
```

Do not add NiceGUI, Excel or other legacy dependencies to `requirements-server.txt` unless an explicit architectural decision requires it.

---

## 9. Python validation

During development, run the smallest tests that directly exercise the changed behavior first.

For a targeted unittest module:

```bash
python -m unittest tests.<module> -v
```

or use another precise unittest target appropriate to the changed code.

Before considering a substantial backend change validated, run relevant neighboring tests.

The CI verification suite is divided into two deterministic shards:

```bash
python tools/run_test_shard.py \
  --shard-index 0 \
  --shard-count 2 \
  --top-slowest 25
```

and:

```bash
python tools/run_test_shard.py \
  --shard-index 1 \
  --shard-count 2 \
  --top-slowest 25
```

Python sources are also compilation-checked with:

```bash
python -m compileall -q app tests tools migrations main.py
```

Do not modify a failing test merely to make CI green.

First determine whether:

- the implementation is incorrect;
- the test represents the intended contract;
- the intended contract deliberately changed.

---

## 10. Server boundary validation

Changes affecting the Web/SQL backend, dependencies, architecture boundaries or migrations should consider the same validations enforced by CI:

```bash
python tools/cutover_inventory.py --check-boundaries
python tools/check_server_dependency_isolation.py
python tools/check_sqlserver_readiness.py
```

The canonical Web/SQL source set includes:

```text
app/application
app/domain
app/infrastructure/sql
app/infrastructure/acumatica
app/infrastructure/m365
app/server
migrations
```

Maintain its isolation from the legacy NiceGUI/Excel runtime.

---

## 11. Frontend and browser validation

CI uses Node 22.

Install frontend dependencies with:

```bash
cd frontend
npm install --prefer-offline --no-audit --no-fund
```

Build:

```bash
npm run build
```

Run browser acceptance tests:

```bash
npm run test:e2e
```

After a production frontend build, the complete FastAPI + React runtime can be validated from the repository root with:

```bash
python tools/check_web_runtime.py
```

When changing a user-visible workflow, prefer exercising the actual browser/API behavior rather than relying only on static source assertions.

---

## 12. Database and migrations

Schema changes use Alembic under `migrations/`.

SQL Server is the reference database for integrated validation, staging and production. SQLite remains permitted for local development and fast tests when the scenario does not depend on database-engine semantics. See `docs/architecture/ADR-011-sql-server-authoritative-database.md`.

A SQLite result alone is not sufficient evidence for behavior that depends on multi-session concurrency, transaction isolation, SQL Server constraints/types, migrations or production performance.

For schema changes:

- create an appropriate forward migration;
- preserve existing data when reasonably possible;
- consider both SQLite behavior and target SQL Server compatibility;
- inspect foreign keys, nullability, uniqueness and cascade behavior;
- update SQLAlchemy models and tests consistently;
- run SQL Server readiness validation.

Do not rewrite an already-shared migration simply to alter current behavior.

Do not perform destructive migrations without explicit approval.

Never change relationship semantics only to make a test pass.

---

## 13. Performance

CI contains explicit V2 API performance budgets.

For changes likely to affect database access, planning projections, API volume or hot endpoints, run:

```bash
python tools/benchmark_v2_api.py --iterations 3 --ci
```

Do not optimize based solely on intuition.

Preserve the project's existing principle of separating:

- database cost;
- external integration cost;
- Python/domain calculation;
- serialization/API work;
- frontend work.

SQLite measurements must not be presented as representative SQL Server production performance.

---

## 14. Privacy and secrets

CI scans the repository for common PII and secrets:

```bash
python tools/privacy_scan.py
```

Never commit:

- passwords;
- API keys;
- access tokens;
- client secrets;
- database credentials;
- SMTP credentials;
- private certificates;
- production configuration secrets;
- local `.env` files;
- local Excel workbooks containing business data.

Do not print secret values in diagnostics.

Use `.env.example` and the existing environment-variable/configuration mechanisms for examples.

---

## 15. Docker validation

Changes affecting Dockerfiles, runtime composition, migrations, frontend serving, startup or deployment should validate Docker configuration:

```bash
docker compose config
```

For substantial runtime changes, use the actual stack when practical:

```bash
docker compose up -d --build
```

The runtime must expose healthy FastAPI/backend behavior and the React application through the configured stack.

Clean up local validation afterward when appropriate:

```bash
docker compose down -v --remove-orphans
```

Do not casually modify deployment behavior merely to satisfy a local test.

---

## 16. CI workflow

The primary PR CI workflow is `.github/workflows/syntax-check.yml`.

Its main jobs are:

```text
server-isolation
python-tests shard 0
python-tests shard 1
frontend-validation
docker-smoke
```

When CI fails:

1. identify the failing job;
2. inspect the exact failing test/command;
3. determine whether the failure is caused by the current change;
4. make the smallest correct fix;
5. run targeted validation locally;
6. push the fix;
7. allow CI to run again;
8. repeat until green.

If auto-merge is enabled on the PR, a red CI must leave the PR open. Do not bypass required checks to force a merge. Resume the PR, correct the failure, and let auto-merge complete only after all required checks are green.

Normal CI failures are part of development and do not require user authorization.

Do not bypass, delete or weaken a meaningful test merely to obtain a green check.

---

## 17. Pre-existing or unrelated failures

If CI appears to fail for a reason unrelated to the current issue:

1. verify that conclusion;
2. check whether the current change exposed a latent defect;
3. determine whether a small, safe correction is necessary to validate the requested work.

Fix the unrelated problem only when the correction is small, well understood and necessary.

Otherwise stop and report:

- failing job/test;
- evidence it is unrelated;
- likely cause;
- impact on the current PR.

---

## 18. Pull requests

A PR should represent one coherent issue or sub-issue.

The PR description should summarize:

- requested change;
- implementation;
- important design decisions;
- tests/validation performed;
- migrations or compatibility implications;
- known limitations, if any.

Do not mix unrelated cleanup into the same PR.

### Auto-merge policy

When repository auto-merge is available and the PR is complete, coherent and ready to merge, enable auto-merge instead of waiting manually for CI to finish.

The normal autonomous flow is:

```text
PR complete
→ enable auto-merge
→ required CI checks run
→ if red: keep PR open, diagnose, fix and rerun
→ if green: GitHub merges automatically
```

Do not enable auto-merge when:

- the user explicitly asked to stop before merge;
- a product or architecture decision is still unresolved;
- the change requires a destructive migration;
- the change introduces an unexpected breaking contract;
- a significant security/authentication decision is still open;
- a required review or repository rule intentionally blocks automatic merge;
- a known blocking defect remains.

Auto-merge is not permission to weaken branch protection, required checks or tests.

Before declaring a PR ready for auto-merge, verify:

- requested behavior is implemented;
- appropriate local/targeted validation has been performed;
- migrations/documentation are updated when required;
- no known blocking defect remains;
- no stop condition from this file applies.

Once auto-merge is armed, the agent does not need to remain idle merely waiting for green CI. If CI later fails during the active task/session, resume the PR and correct the failure. If the session is interrupted, the PR must remain safely open unless all required repository checks become green and GitHub merges it automatically.

When the task has been approved for autonomous development, a PR that satisfies all required repository checks may be merged automatically without asking for another confirmation unless the user explicitly requested a stop before merge.

If repository protection, required approval or another rule prevents merging, report the blocker rather than circumventing it.

---

## 19. GitHub roadmap updates

The current roadmap is represented primarily through GitHub Issues and their related sub-items/checklists.

There is currently no canonical `ROADMAP.md`.

After merging an issue/sub-issue:

- update the relevant GitHub Issue or roadmap checklist when needed;
- mark only work that is actually complete;
- reference the merged PR when useful;
- do not rewrite unrelated roadmap priorities;
- do not invent a new roadmap state system unless explicitly requested.

A task is not complete merely because code exists on a branch.

### Dev Cockpit pipeline contract

When roadmap #55 contains a versioned `COCKPIT_PIPELINE_V1` block, that block is the machine-readable contract used by the Dev Cockpit. Human roadmap prose, tables, PR numbers, CI numbers and commit SHAs must never be used as substitute step identities.

Any change to the work order or completion state in #55 must:

- update `COCKPIT_PIPELINE_V1` in the same roadmap edit;
- preserve stable issue/sub-slice identity (`399`, `399A`, etc.);
- mark a merged/completed step `DONE` and promote the actual next MAIN step to `READY`;
- keep later MAIN steps `BLOCKED`;
- keep the human roadmap text consistent with the canonical block;
- never use a PR, CI run or commit number as `KEY`;
- verify after the roadmap edit that the canonical parser resolves the expected active step.

Do not store the current next issue or current active step in this file. AGENTS.md contains only the durable rules of the contract; #55 contains the current product state.

If a canonical block exists but is invalid, do not work around it by editing the cockpit heuristics or by inferring a different active step from surrounding Markdown. Correct #55 so the canonical contract becomes valid again.

### Dev Cockpit Safe Writeback

The Dev Cockpit may apply a roadmap reconciliation to #55 only through the explicit Safe Writeback flow.

Durable invariants:

- preview is mandatory before apply;
- apply must require explicit confirmation;
- the backend must recompute the current Reconciler proposal instead of trusting a pipeline block sent by the browser;
- only the exact `COCKPIT_PIPELINE_V1` block may change; all Markdown outside the block must remain byte-for-byte unchanged;
- writeback must fail closed when the pipeline is legacy, invalid, non-stale, or the proposal is no longer the one previewed;
- use optimistic concurrency against the complete issue body and GitHub `updated_at`; a concurrent roadmap edit must produce a conflict rather than being overwritten;
- never auto-complete architecture or environment gates from PR/CI evidence;
- never trigger writeback from dashboard polling, refresh, Attention Center, or agent activity;
- GitHub remains the source of truth; no local roadmap mutation state may be introduced to make writeback easier.

A writeback conflict must be resolved by refreshing GitHub state and reviewing a new preview. Do not bypass the conflict check.

### Dev Cockpit Flow Analytics

Flow Analytics is an observational projection of GitHub delivery history. It must not become a second historical state store.

Durable invariants:

- derive delivery identity from canonical WORK keys and the same strict PR identity rule used by the Roadmap Reconciler;
- exclude docs-only PRs from DEV delivery metrics;
- use only timestamps explicitly returned by GitHub for commits, PRs, workflows/jobs and merges;
- never invent branch creation timestamps, historical stall intervals or historical #55 reconciliation timestamps;
- when a metric cannot be reconstructed reliably, return it as unavailable/partial rather than estimating it;
- keep historical analytics off the 60-second dashboard polling path; load it separately and on demand;
- bound historical GitHub API work so analytics cannot dominate normal cockpit traffic;
- do not persist derived delivery phases, analytics snapshots or scores locally merely to make historical charts easier;
- do not introduce opaque productivity scores or use analytics to change roadmap order automatically.

Analytics may describe bottlenecks and trends from observable durations, but it does not alter product state or authorize writeback.

---

## 20. Chained execution

When explicitly authorized to execute a roadmap block, the agent may proceed automatically from one sub-item to the next.

Example:

```text
331A
→ implementation
→ CI
→ fixes if needed
→ green
→ merge
→ roadmap update
→ synchronize main
→ 331B
```

Automatic chaining is allowed only when the next item:

- is already clearly defined;
- belongs to the same approved work block;
- does not require a new product decision;
- does not require an unresolved architectural decision.

Always synchronize with `main` after the previous PR is merged before beginning the next sub-item.

Do not automatically consume arbitrary backlog issues outside the approved block.

---

## 21. Stop conditions

Stop and request a decision when:

- the business requirement is materially ambiguous;
- several incompatible business behaviors are plausible;
- implementation requires a significant architecture change not already approved;
- a destructive migration is required;
- an unexpected breaking API change is required;
- a substantial new dependency is required;
- security implications are unclear or significant;
- the requested behavior conflicts with existing documented architecture;
- an unrelated defect prevents reliable validation;
- CI remains unresolved after reasonable targeted investigation;
- the next roadmap item is insufficiently defined.

Do not stop for:

- ordinary coding problems;
- normal debugging;
- normal test failures caused by the implementation;
- lint/type/build errors;
- straightforward merge conflicts;
- small refactors necessary to implement the requested behavior.

---

## 22. Architecture escalation package

When an architecture decision is genuinely required, stop implementation before building several speculative alternatives.

Provide:

### Problem
What cannot safely be decided during normal implementation.

### Current behavior
What the repository currently does.

### Requested behavior
What the issue requires.

### Relevant code
Files, models, services, APIs and tests involved.

### Constraints
Data compatibility, migrations, API contracts, security, performance or deployment constraints.

### Options
The realistic implementation options found during investigation.

### Decision required
One precise architectural question.

This package should allow an architect/senior reviewer to analyze the problem without repeating the developer's entire exploration.

---

## 23. Documentation

Update durable documentation when behavior or architecture materially changes.

Relevant existing documentation is under `docs/`.

Do not create documentation merely to restate obvious implementation details.

If a new long-term architectural decision has important alternatives or consequences, document it in an appropriate architecture/decision document rather than leaving it only in a PR or conversation.

---

## 24. Definition of Done

For normal autonomous implementation work, an item is DONE only when:

- the requested behavior is implemented;
- appropriate targeted tests pass;
- relevant regression tests pass;
- required CI is green;
- the PR is merged;
- required migrations are present;
- durable documentation is updated when necessary;
- the relevant GitHub roadmap/issue state reflects reality;
- no known blocker related to the change remains.

`Code complete` and `CI started` are not definitions of done.

---

## 25. Communication during execution

Keep progress updates concise.

Useful checkpoints are:

- implementation completed; local tests running;
- PR created; CI running;
- CI failed; cause identified;
- correction pushed;
- CI green;
- PR merged;
- roadmap updated;
- next approved sub-item started;
- architecture/product decision required.

Do not produce lengthy status reports for routine implementation details.

---

## 26. Guiding principle

Prefer:

```text
small
+ coherent
+ testable
+ reversible
+ documented when necessary
```

over:

```text
large speculative refactors
```

The goal is not merely to write code.

The goal is to move an approved development item safely from requirement to merged, validated functionality.
