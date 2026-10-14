# #750 - Planning excavation category acceptance

## Baseline and shared cause

Investigated on `main@4938968097156cdb2e61cc42a2a2701fecca75a3`, which
already contains #749 / PR #751. The preceding main commit was
`12280629ad76057af1c05aec758776c8f784c0e8`.

The repository-level omission is in the **backend resource projection**, not a
React class whitelist. Before #751, both the global Planning snapshot and capacity
grid selected `list_schedulable_resources(start, end)`. That query requires an
active standard schedule overlapping the displayed window, in addition to active
Resource / ERP flags. If every active resource in a class lacks that schedule,
the entire class disappears from the resource projection. React consequently has
no resource row from which to create the class group. This is the whole-class
version of #749's partial `INSTALL_ELEC` membership defect.

#751 already changed the two global projections to `list_resources(active_only=True)`.
This #750 delivery therefore adds acceptance coverage and the trace below, **not
a second runtime fix**. Resources without schedules stay visible but unavailable;
no capacity, schedule, master class, permission, or mutation policy is invented.

This is a reproducible code-path explanation, not a claim to have inspected the
production database. The production resources' actual schedules, configured class
label, selected scope, and deployed application version were not available here.

## End-to-end trace

1. `Resource.resource_class` holds the resource's canonical class code. Planning
   projects this field directly; it is not inferred from an ERP task, employee
   display name, login, or `ResourceProfile` join.
2. `SqlPlannerQueryRepository.list_resources(active_only=True)` selects all rows
   with both `Resource.active` and `Resource.erp_active`. It has no class whitelist,
   per-class limit, schedule requirement, or class-keyed dictionary overwrite.
3. `routes_reads.py` resolves `PlanningVisibilityService` before selecting global
   or scoped queries. Global access remains restricted by #618 / ADR-027.
   Scoped queries still use the resolved project, request, resource, and exact
   technician project/day relationships. Direct coordination uses stable contact
   IDs and does not require a Shift or standard schedule.
4. The snapshot serializes each resource's code as `resource_class`. The separate
   `/api/v1/resource-classes` projection supplies code/label/active options from
   `ResourceClassConfig` through `SqlResourceClassRepository`. `INSTALL_EXC` is a
   normal configured string code, not a special frontend enum value.
5. `PlanningPage.tsx` constructs class groups and filter options from the authorized
   snapshot resources and existing visible unplanned work. It appends all resources
   to arrays keyed by class code; labels do not define membership. The class catalog
   provides presentation labels, not authorization or a list of groups to fabricate.
6. The `INSTALL_EXC` group displays the configured label `Installation excavation`.
   Existing #712B behavior remains: unavailable/unresolved labels fall back to
   `Non classé` without deleting or merging code-keyed
   groups. An absent label must not be repaired with a one-off React mapping.

A configured class with no visible resources and no visible unplanned work must
not create an empty group. Existing candidate/assignment-only groups remain valid;
#750 does not change that separate Planning contract.

## Regression coverage

`tests/test_planning_excavation_750.py` reuses `SqliteDatabaseTemplate`, the real
SQL repositories, FastAPI `TestClient`, and the existing static-auth pattern:

- Three active `INSTALL_EXC` resources have no schedules or shifts. A scheduled
  `INSTALL_ELEC` resource is the control. The former schedulable catalog contains
  only the control, but the actual global snapshot and capacity API must contain
  all four exactly once; unavailable excavation capacity remains zero.
- The real class-options API preserves both configured display labels. A configured
  empty class is present in that catalog but absent from snapshot resource classes.
- Two excavation coordinators receive different subsets of the same class. A third
  coordinator receives only the electrical control. Out-of-scope, inactive, and
  ERP-inactive resource identifiers must be absent from the scoped JSON.
- Unlinked project managers and technicians receive empty personal projections;
  explicit global snapshot/capacity requests are rejected with 403.

`frontend/e2e/issue-712b.spec.ts` extends the existing browser contract tests:

- A backend-shaped projection with three excavation resources generates exactly
  one `Installation excavation` group, alongside `Installation électrique`, with all resources and correctly labeled class filters.
- An electrical-only projection does not gain excavation rows or an excavation
  group/filter merely because the class-options and editor resource catalogs
  still contain excavation. Catalog-only empty classes remain absent.

The browser tests mock projections to isolate rendering; the Python acceptance
tests separately exercise real SQL, API serialization, and scope resolution.
Neither set replaces the existing #618 security acceptance suite.

## Validation

Targeted commands in a complete checkout:

```bash
python -m unittest tests.test_planning_excavation_750 -v
cd frontend
npx playwright test e2e/issue-712b.spec.ts --grep 750
```

Neighboring validation:

```bash
python -m unittest tests.test_sql_query_repository tests.test_planning_visibility tests.test_planning_visibility_security_acceptance tests.test_server_read_routes -q
python -m compileall -q app tests tools migrations main.py
python tools/cutover_inventory.py --check-boundaries
python tools/check_server_dependency_isolation.py
python tools/check_sqlserver_readiness.py
python tools/privacy_scan.py
cd frontend
npm run build
npx playwright test e2e/issue-712b.spec.ts
```

DEV environment results: Python syntax compilation and TypeScript transpilation
passed. Four isolated SQL-predicate checks reproduced the old whole-class omission
and the current active-master/scope behavior; four isolated checks of the inspected
React grouping callback passed. These are limited diagnostic checks, not full
repository API/browser execution or SQL Server validation.

The canonical API test command was attempted but could not import `app`: shell
access to GitHub failed DNS resolution, so a complete checkout was unavailable.
The full API, neighboring, browser, build, and boundary suites remain for the normal
required GitHub Actions checks. No CI settings or checks are weakened by this PR.

## Production verification / remaining uncertainty

After deployment of the shared #751 fix, use the same authorized identity, date
window, and scope to compare the `INSTALL_EXC` resource IDs in the Planning snapshot
with the visible grid. Verify the class-options response contains the configured
label, and clear client search/project/confirmation/capacity filters for the check.
Repeat with a coordinator who has no excavation resources: neither their scoped
response nor the resource grid should acquire an excavation group from catalogs.

If production still differs, inspect its deployment version, canonical class code
and label, active/ERP-active flags, and resolved scope before changing code or data.
This delivery does not assert that production has already been verified, mark #750
DONE in #55, or start #709B. #749's runtime fix is reused without duplication.
