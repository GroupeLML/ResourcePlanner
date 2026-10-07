import { expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

type Phase = "candidate" | "partial" | "covered";

test("693 projette le même besoin par classe et dans la file jusqu'à couverture réelle", async ({ browser }) => {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "COORDINATOR" },
  });
  const page = await context.newPage();
  let phase: Phase = "candidate";

  await page.route("**/api/v1/planning/snapshot?**", async (route) => {
    const response = await route.fetch();
    const body = await response.json() as Record<string, any>;
    const start = String(body.start);
    const end = String(body.end);

    body.pending_loads = phase === "candidate"
      ? [{
        demand_number: "DMO-693",
        project_number: "P-693",
        project_name: "Projet file par classe",
        start_date: start,
        end_date: end,
        projected_hours: 12,
        window_hours: 12,
        mode: "ADDITIVE",
        load_kind: "POTENTIAL",
        current_plan_hours: 0,
        delta_hours: null,
        resource_count: 1,
        required_competencies: "PLC",
        proposed_resource: null,
        work_package_ref: null,
        confirmation: "Tentative",
        periods: [],
        candidate_windows: [{
          candidate_key: "DMO-693:LINE-1:window",
          start_date: start,
          end_date: end,
          projected_hours: 12,
          window_hours: 12,
          proposed_resource_id: null,
          proposed_resource: null,
          task_code: "310",
          task_label: "Programmation",
          required_resource_class: "PROGRAMMATION",
          required_competencies: "PLC",
          confirmation: "Tentative",
          resource_count: 1,
          period_kind: null,
          alternative_group: null,
          selected: false,
        }],
      }]
      : [];

    await route.fulfill({
      response,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  });

  await page.route("**/api/v1/planning/actions**", async (route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    const url = new URL(route.request().url());
    const start = url.searchParams.get("start") || "2026-10-05";
    const end = url.searchParams.get("end") || start;
    const common = {
      demand_number: "DMO-693",
      project_number: "P-693",
      project_name: "Projet file par classe",
      task_code: "310",
      task_label: "Programmation",
      start_date: start,
      end_date: end,
      required_resource_class: "PROGRAMMATION",
      required_competency: "PLC",
      required_competency_id: "C-PLC",
      priority: "Haute",
      status: phase === "candidate" ? "Soumise" : "À assigner",
      confirmation: phase === "candidate" ? "Tentative" : "Confirmée",
      project_manager: "CP 693",
      requester: "Demandeur 693",
      emergency_override_active: false,
    };
    const rows = phase === "candidate"
      ? [{
        ...common,
        kind: "APPROVAL",
        reference: "DMO-693",
        segment_id: null,
        planned_hours: 12,
      }]
      : phase === "partial"
        ? [{
          ...common,
          kind: "ASSIGNMENT",
          reference: "SEG-693",
          segment_id: "SEG-693",
          planned_hours: 6,
        }]
        : [];

    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(rows),
    });
  });

  try {
    await page.goto("/");

    const workQueue = page.locator(".planning-work-queue");
    await expect(workQueue).toContainText("À approuver");
    await expect(workQueue).toContainText("À planifier");
    await expect(page.locator(".planning-action-panel")).toHaveCount(1);

    const candidate = page.locator(".pending-ghost-card").filter({ hasText: "DMO-693" }).first();
    await expect(candidate).toBeVisible();
    await expect(candidate).toContainText("en attente d’approbation");
    await expect(candidate).toHaveAttribute("draggable", "false");
    await expect(page.locator(".resource-group-heading").filter({ hasText: "PROGRAMMATION" })).toBeVisible();

    phase = "partial";
    await page.reload();

    await expect(page.locator(".pending-ghost-card").filter({ hasText: "DMO-693" })).toHaveCount(0);
    const approved = page.locator(".approved-unplanned-card").filter({ hasText: "DMO-693" }).first();
    await expect(approved).toBeVisible();
    await expect(approved).toContainText("Reliquat 6 h");
    await expect(approved).toHaveAttribute("draggable", "true");
    await expect(workQueue).toContainText("Approuvé · reliquat à couvrir");

    const classHeading = page.locator(".resource-group-heading").filter({ hasText: "PROGRAMMATION" });
    await classHeading.click();
    await expect(approved).toBeHidden();
    await classHeading.click();
    await expect(approved).toBeVisible();

    phase = "covered";
    await page.reload();

    await expect(page.locator(".approved-unplanned-card").filter({ hasText: "DMO-693" })).toHaveCount(0);
    await expect(page.locator(".pending-ghost-card").filter({ hasText: "DMO-693" })).toHaveCount(0);
    await expect(workQueue).toContainText("Aucune action de planification");
  } finally {
    await context.close();
  }
});
