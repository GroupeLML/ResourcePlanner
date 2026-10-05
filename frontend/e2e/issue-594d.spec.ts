import { Browser, BrowserContext, Page, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

async function openCoordinator(browser: Browser) {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "COORDINATOR" },
  });
  const page = await context.newPage();
  await page.goto("/");
  await expect(page.locator(".sidebar-footer")).toContainText("Coordonnateur E2E");
  return { context, page };
}

async function navigateMain(page: Page, label: string) {
  await page.locator(".main-nav").getByRole("button", { name: new RegExp(label, "i") }).click();
}

test("594D affiche le responsable effectif du périmètre coordonnateur sans élargir la projection", async ({ browser }) => {
  const { context, page } = await openCoordinator(browser);
  try {
    await page.route("**/api/v1/coordinator-dashboard", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          as_of: "2026-10-06",
          attention_horizon_days: 7,
          kpis: {
            personal_demands: 1,
            total_actions: 1,
            assignments: 1,
            cancellations: 0,
            approvals: 0,
            coverage_issues: 0,
            partial_coverages: 0,
            conflicts: 0,
            attention_items: 0,
          },
          personal_demands: [
            {
              demand_number: "DMO-594D",
              project_number: "P-594D",
              project_name: "Projet 594D",
              effective_status: "En planification",
              priority: "Normale",
              desired_start: "2026-10-06",
              desired_end: "2026-10-06",
              cancellation_pending: false,
              attention: "NORMAL",
              days_until_start: 0,
            },
          ],
          actions: [
            {
              action_id: "WORKFORCE_ASSIGNMENT:SEG-594D",
              category: "ASSIGNMENT",
              kind: "WORKFORCE_ASSIGNMENT",
              label: "Attribuer une ressource",
              detail: "210 · Automatisation",
              demand_number: "DMO-594D",
              source_id: "SEG-594D",
              project_number: "P-594D",
              project_name: "Projet 594D",
              status: "Planifié",
              priority: "Normale",
              start_date: "2026-10-06",
              end_date: "2026-10-06",
              planned_hours: 8,
              covered_hours: null,
              remaining_hours: 8,
              attention: "NORMAL",
              days_until_start: 0,
              target: "PLANNING",
              resource_kind: "WORKFORCE",
              related_ids: [],
              operational_responsible_contact_id: "C-594D",
              operational_responsible_display_name: "Responsable 594D",
              operational_responsible_status: "RESOLVED",
              operational_responsible_source_type: "TASK_RESPONSIBLE",
              operational_responsible_source_label: "Tâche 210",
            },
          ],
        }),
      });
    });

    await navigateMain(page, "Coordonnateur");

    await expect(page.getByRole("heading", { name: "Tableau de bord coordonnateur" })).toBeVisible();
    await expect(page.getByText("Responsable : Responsable 594D · source tâche")).toBeVisible();
    await expect(page.getByText("DMO-594D", { exact: true })).toHaveCount(2);
    await expect(page.getByText("demandes actives", { exact: true })).toBeVisible();
    await expect(page.locator(".coordinator-kpi-grid").getByText("1", { exact: true }).first()).toBeVisible();
  } finally {
    await context.close();
  }
});
