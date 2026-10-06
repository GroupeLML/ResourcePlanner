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

test("#556/#614F compacte le Gantt et bascule les budgets en heures de façon cohérente", async ({ browser }) => {
  const { context, page } = await openAdmin(browser);

  try {
    await page.route("**/api/v1/medium-term/budget?**", async (route) => {
      const response = await route.fetch();
      const payload = await response.json() as Record<string, unknown>;
      await route.fulfill({
        response,
        json: {
          ...payload,
          project_id: null,
          project_number: null,
          project_name: null,
          project_count: 1,
          diagnostics: [],
          weekly_diagnostics: [],
          tasks: [
            {
              task_catalog_item_id: "TASK-556-216",
              task_code: "216",
              task_label: "Programmation",
              erp_task_id: "ERP-556-216",
              account_group: "DEPMO",
              budget_amount_cad: 50000,
              budget_actual_cad: 31000,
              remaining_budget_cad: 19000,
              financial_diagnostic: null,
              average_hourly_cost_cad: 100,
              remaining_budget_hours_from_actual: 190,
              actual_hours_diagnostic: null,
              future_work_package_hours: 0,
              future_work_package_diagnostic: null,
              remaining_after_work_packages_hours: 190,
              budget_hours: 500,
              planned_wp_hours: 0,
              remaining_budget_hours: 500,
              remaining_reference_date: "2026-10-01",
              remaining_reference_basis: "ERP_TASK_BUDGET_LAST_SUCCESS_DATE",
              remaining_work_package_hours: 0,
              remaining_structured_balance_hours: 190,
              remaining_mode_diagnostics: [],
              associated_work_package_count: 0,
              budget_included_work_package_count: 0,
              diagnostic_state: "NO_WORK_PACKAGES",
              budget_source_diagnostic: null,
              work_packages: [],
              active: true,
              workforce_eligible: true,
              project_id: "P-251-ID",
              project_number: "P-251",
              project_name: "Projet E2E",
              project_manager_contact_id: "BC-PM-556",
              project_manager_display_name: "Benjamin Germain",
              manager_group_key: "erp:EMP-PM",
              manager_display_name: "Benjamin Germain",
              manager_resolution_status: "RESOLVED",
              manager_diagnostics: [],
              erp_budget_last_success_at: "2026-10-01T13:42:00Z",
            },
          ],
          task_options: [
            {
              task_catalog_item_id: "TASK-556-216",
              project_id: "P-251-ID",
              project_number: "P-251",
              project_name: "Projet E2E",
              task_code: "216",
              task_label: "Programmation",
            },
          ],
          resource_classes: [
            { code: "PROGRAMMEUR", label: "Programmeur", active: true },
          ],
          weeks: [
            {
              week_start: "2026-09-28",
              work_package_hours: 140,
              capacity_hours: 240,
              utilization: 58,
              state: "available",
              diagnostics: [],
              classes: [
                {
                  resource_class_code: "PROGRAMMEUR",
                  resource_class_label: "Programmeur",
                  capacity_hours: 240,
                  work_package_hours: 140,
                  utilization: 58,
                  state: "available",
                  diagnostics: [],
                },
              ],
            },
          ],
        },
      });
    });

    await navigateMain(page, "Moyen terme");

    const capacityClass = page.locator(".mt-capacity-label.is-class");
    await expect(capacityClass).toHaveCount(1);
    await expect(capacityClass).toContainText("Programmeur");
    const capacityCell = page.locator(".mt-capacity-cell.is-compact").first();
    await expect(capacityCell).toContainText(/140\s?h/);
    await expect(capacityCell).toContainText(/240\s?h/);
    await expect(capacityCell).toContainText(/58\s?%/);
    await expect(capacityCell).toContainText("Disponible");

    const manager = page.getByRole("button", { name: /Benjamin Germain/ });
    await expect(manager).toBeVisible();
    const project = page.getByRole("button", { name: /P-251.*Projet E2E/ });
    await expect(project).toBeVisible();

    const task = page.locator(".mt-task-group").filter({ hasText: "Programmation" }).first();
    await expect(task).toContainText("Budget initial");
    await expect(task).toContainText("500 h");
    await expect(task).toContainText("Charge WP");
    await expect(task).toContainText("0 h");
    await expect(task).toContainText("Solde structuré");
    await expect(task).toContainText("ERP synchronisé");

    await page.getByRole("combobox", { name: "Mode budget Moyen terme" }).selectOption("remaining");
    await expect(task).toContainText("Budget restant");
    await expect(task).toContainText("190 h");
    await expect(task).toContainText("Cutoff ERP inclusif 2026-10-01");

    await project.click();
    await expect(task).toBeHidden();
    await project.click();
    await expect(task).toBeVisible();

    await manager.click();
    await expect(project).toBeHidden();
    await manager.click();
    await expect(project).toBeVisible();

    const flowOrder = await page.locator(".mt-board, .mt-unlinked-panel").evaluateAll(
      (nodes) => nodes.map((node) => node.className),
    );
    expect(flowOrder[0]).toContain("mt-board");
    expect(flowOrder[flowOrder.length - 1]).toContain("mt-unlinked-panel");
  } finally {
    await closeContext(context);
  }
});
