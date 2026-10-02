import { Browser, BrowserContext, Page, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

async function openAdmin(browser: Browser) {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "ADMIN" },
  });
  const page = await context.newPage();
  await page.goto("/");
  return { context, page };
}

async function closeContext(context: BrowserContext) {
  await context.close();
}

async function navigateMain(page: Page, label: string) {
  await page.locator(".main-nav").getByRole("button", { name: new RegExp(label, "i") }).click();
}

test("Projet compare BudgetActual et charge WorkPackage future depuis le lundi", async ({ browser }) => {
  const { context, page } = await openAdmin(browser);

  try {
    await page.route("**/api/v1/medium-term/budget?**", async (route) => {
      const response = await route.fetch();
      const payload = await response.json() as {
        tasks: Array<Record<string, unknown>>;
        erp_budget_last_success_at: string | null;
      };
      await route.fulfill({
        response,
        json: {
          ...payload,
          reference_week_start: "2026-09-28",
          actual_through_date: "2026-09-27",
          reference_basis: "ERP_BUDGET_ACTUAL_THROUGH_PREVIOUS_WEEK",
          erp_budget_last_success_at: "2026-10-01T13:42:00Z",
          tasks: payload.tasks.map((task, index) => (
            index === 0
              ? {
                  ...task,
                  budget_amount_cad: 50000,
                  budget_actual_cad: 31000,
                  remaining_budget_cad: 19000,
                  financial_diagnostic: null,
                  average_hourly_cost_cad: 100,
                  remaining_budget_hours_from_actual: 190,
                  actual_hours_diagnostic: null,
                  future_work_package_hours: 150,
                  future_work_package_diagnostic: null,
                  remaining_after_work_packages_hours: 40,
                }
              : task
          )),
        },
      });
    });

    await navigateMain(page, "Projets");
    const projectRow = page.locator(".projects-table tbody tr").filter({ hasText: "P-251" }).first();
    await expect(projectRow).toBeVisible();
    await projectRow.getByRole("button", { name: "Ouvrir" }).click();

    const detail = page.locator(".project-contact-admin");
    await expect(detail).toContainText("Référence hebdomadaire");
    await expect(detail).toContainText("28 septembre 2026");
    await expect(detail).toContainText("BudgetActual considéré jusqu’au 27 septembre 2026");
    await expect(detail).toContainText("Budgets ERP synchronisés");
    await expect(detail).not.toContainText("Dernières heures approuvées");

    const budgets = detail.locator(".project-task-contact-list").filter({ hasText: "Budgets tâches ERP" }).first();
    await expect(budgets.getByRole("columnheader", { name: "Budget ERP" })).toBeVisible();
    await expect(budgets.getByRole("columnheader", { name: "Actual ERP" })).toBeVisible();
    await expect(budgets.getByRole("columnheader", { name: "Restant ERP" })).toBeVisible();
    await expect(budgets.getByRole("columnheader", { name: "Coût moyen" })).toBeVisible();
    await expect(budgets.getByRole("columnheader", { name: "Budget restant selon Actual" })).toBeVisible();
    await expect(budgets.getByRole("columnheader", { name: "Charge WP à partir du lundi" })).toBeVisible();
    await expect(budgets.getByRole("columnheader", { name: "Marge après charge future" })).toBeVisible();
    await expect(budgets.getByRole("columnheader", { name: "Solde de structuration" })).toBeVisible();

    const taskRow = budgets.locator("tbody tr").first();
    await expect(taskRow).toContainText(/50\s?000/);
    await expect(taskRow).toContainText(/31\s?000/);
    await expect(taskRow).toContainText(/19\s?000/);
    await expect(taskRow).toContainText("100");
    await expect(taskRow).toContainText("190 h");
    await expect(taskRow).toContainText("150 h");
    await expect(taskRow).toContainText("40 h");
    await expect(taskRow).toContainText("Aucun WorkPackage associé");
  } finally {
    await closeContext(context);
  }
});
