import { expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

test("704 affiche une candidate sous la ressource proposée sans créer de quart et conserve le fallback", async ({ browser }) => {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "COORDINATOR" },
  });
  const page = await context.newPage();
  let unresolved = false;
  let resourceId = "";
  let day = "";

  await page.route("**/api/v1/planning/snapshot?**", async (route) => {
    const response = await route.fetch();
    const body = await response.json() as Record<string, any>;
    const resource = (body.resources as Record<string, any>[]).find((item) => Boolean(item.id));
    if (!resource) throw new Error("704 E2E requires a planning resource fixture");
    resourceId = String(resource.id);
    day = String(body.start);
    const resourceClass = resource.resource_class || "Non classé";

    body.pending_loads = [{
      demand_number: "DMO-704",
      project_number: "P-704",
      project_name: "Projet ressource proposée",
      start_date: day,
      end_date: String(body.end),
      projected_hours: 8,
      window_hours: 8,
      mode: "ADDITIVE",
      load_kind: "POTENTIAL",
      current_plan_hours: 0,
      delta_hours: null,
      resource_count: 1,
      required_competencies: null,
      proposed_resource: resource.name,
      work_package_ref: null,
      confirmation: "Confirmée",
      periods: [],
      candidate_windows: [{
        candidate_key: "DMO-704:LINE-1:window",
        start_date: day,
        end_date: String(body.end),
        projected_hours: 8,
        window_hours: 8,
        proposed_resource_id: unresolved ? "MISSING-704" : resourceId,
        proposed_resource: resource.name,
        task_code: "704",
        task_label: "Installation",
        required_resource_class: resourceClass,
        required_competencies: null,
        confirmation: "Confirmée",
        resource_count: 1,
        period_kind: null,
        alternative_group: null,
        selected: false,
      }],
    }];
    await route.fulfill({ response, contentType: "application/json", body: JSON.stringify(body) });
  });

  try {
    await page.goto("/");
    const candidate = page.locator(`.planning-day-cell[data-resource-id="${resourceId}"][data-day="${day}"] .pending-ghost-card`)
      .filter({ hasText: "DMO-704" });
    await expect(candidate).toBeVisible();
    await expect(candidate).toContainText("en attente d’approbation");
    await expect(candidate).toHaveAttribute("draggable", "false");
    await expect(page.locator(".unplanned-candidate-row .pending-ghost-card").filter({ hasText: "DMO-704" })).toHaveCount(0);

    unresolved = true;
    await page.reload();
    await expect(page.locator(`.planning-day-cell[data-resource-id="${resourceId}"] .pending-ghost-card`)
      .filter({ hasText: "DMO-704" })).toHaveCount(0);
    await expect(page.locator(".unplanned-candidate-row .pending-ghost-card")
      .filter({ hasText: "DMO-704" }).first()).toBeVisible();
  } finally {
    await context.close();
  }
});
