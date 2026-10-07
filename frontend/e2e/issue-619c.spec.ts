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

async function navigateMain(page: Page, label: string) {
  await page.locator(".main-nav").getByRole("button", { name: new RegExp(label, "i") }).click();
}

test("#619C replie les classes et expose les compétences non additives", async ({ browser }) => {
  const { context, page } = await openAdmin(browser);

  try {
    await page.route("**/api/v1/medium-term/budget?**", async (route) => {
      await route.fulfill({
        json: {
          project_id: null,
          project_number: null,
          project_name: null,
          tasks: [],
          reference_week_start: "2026-10-05",
          actual_through_date: null,
          reference_basis: "WINDOW",
          erp_budget_last_success_at: null,
          unclassified_work_packages: [],
          diagnostics: [],
          weekly_diagnostics: [],
          window_start: "2026-10-05",
          window_end: "2026-10-11",
          project_count: 1,
          task_options: [],
          resource_classes: [
            { code: "AUTOMATION", label: "Automatisation", active: true },
          ],
          weeks: [
            {
              week_start: "2026-10-05",
              work_package_hours: 16,
              capacity_hours: 60,
              utilization: 27,
              state: "available",
              diagnostics: [],
              classes: [
                {
                  resource_class_code: "AUTOMATION",
                  resource_class_label: "Automatisation",
                  capacity_hours: 60,
                  work_package_hours: 16,
                  utilization: 27,
                  state: "available",
                  diagnostics: [],
                },
              ],
              competencies: [
                {
                  competency_id: "C-PLC",
                  competency_name: "PLC",
                  competency_active: true,
                  resource_class_code: "AUTOMATION",
                  resource_class_label: "Automatisation",
                  resource_class_active: true,
                  requested_hours: 48,
                  capacity_hours: 52,
                  utilization: 92,
                  state: "warning",
                  diagnostics: [],
                  load_source: "CURRENT_WORKFORCE_DEMAND",
                  capacity_basis: "GROSS_AVAILABILITY",
                  non_additive: true,
                  qualifying_resource_count: 2,
                },
                {
                  competency_id: "C-SCADA",
                  competency_name: "SCADA",
                  competency_active: true,
                  resource_class_code: "AUTOMATION",
                  resource_class_label: "Automatisation",
                  resource_class_active: true,
                  requested_hours: 40,
                  capacity_hours: 32,
                  utilization: 125,
                  state: "overloaded",
                  diagnostics: ["COMPETENCY_CAPACITY_EXCEEDED"],
                  load_source: "CURRENT_WORKFORCE_DEMAND",
                  capacity_basis: "GROSS_AVAILABILITY",
                  non_additive: true,
                  qualifying_resource_count: 1,
                },
              ],
              competency_combinations: [
                {
                  competency_ids: ["C-PLC", "C-SCADA"],
                  competency_names: ["PLC", "SCADA"],
                  required_resource_class_code: "AUTOMATION",
                  requested_hours: 40,
                  common_capacity_hours: 32,
                  utilization: 125,
                  state: "overloaded",
                  diagnostics: ["COMPETENCY_COMMON_QUALIFICATION_EXCEEDED"],
                  demand_line_count: 1,
                  capacity_basis: "GROSS_AVAILABILITY",
                  non_additive: true,
                  advisory_only: true,
                },
              ],
              competency_diagnostics: [],
            },
          ],
        },
      });
    });

    await navigateMain(page, "Moyen terme");

    await expect(page.getByText("Compétences non additives.")).toBeVisible();
    await expect(page.locator(".mt-capacity-label.is-competency")).toHaveCount(0);

    const classToggle = page.getByRole("button", { name: /Automatisation.*2 compétence/i });
    await expect(classToggle).toHaveAttribute("aria-expanded", "false");
    await classToggle.click();
    await expect(classToggle).toHaveAttribute("aria-expanded", "true");

    const competencyRows = page.locator(".mt-capacity-label.is-competency");
    await expect(competencyRows).toHaveCount(2);
    await expect(competencyRows.filter({ hasText: "PLC" })).toBeVisible();
    await expect(competencyRows.filter({ hasText: "SCADA" })).toBeVisible();

    const skillCells = page.locator(".mt-capacity-cell.is-competency");
    await expect(skillCells.nth(0)).toContainText(/48\s?h demandées/);
    await expect(skillCells.nth(0)).toContainText(/52\s?h capacité théorique/);
    await expect(skillCells.nth(0)).toContainText("2 ressource(s) qualifiée(s)");
    await expect(skillCells.nth(1)).toContainText(/40\s?h demandées/);
    await expect(skillCells.nth(1)).toContainText(/32\s?h capacité théorique/);
    await expect(skillCells.nth(1)).toContainText("Surchargé");
    await expect(skillCells.nth(1)).toContainText("Heures demandées supérieures à la capacité théorique");

    await expect(page.getByText(/Qualification commune PLC \+ SCADA/)).toBeVisible();
    await expect(page.getByText(/Diagnostic indicatif et non additif/)).toBeVisible();

    const filteredRequest = page.waitForRequest((request) =>
      request.url().includes("competency_resource_class_code=AUTOMATION"),
    );
    await page
      .getByRole("combobox", { name: "Classe de regroupement des compétences" })
      .selectOption("AUTOMATION");
    await filteredRequest;
  } finally {
    await context.close();
  }
});
