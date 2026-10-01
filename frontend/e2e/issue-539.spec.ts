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

test("Projet distingue budget ERP CAD, heures structurées et cutoff indisponible", async ({ browser }) => {
  test.setTimeout(120_000);
  const { context, page } = await openAdmin(browser);

  try {
    await navigateMain(page, "Projets");
    const projectRow = page.locator(".projects-table tbody tr").filter({ hasText: "P-251" }).first();
    await expect(projectRow).toBeVisible();
    await projectRow.getByRole("button", { name: "Ouvrir" }).click();

    const detail = page.locator(".project-contact-admin");
    await expect(detail).toContainText("Dernières heures approuvées");
    await expect(detail).toContainText("Indisponible");
    await expect(detail).toContainText("filtre temporel des WorkPackages non appliqué");

    const syncResponse = page.waitForResponse((response) => (
      response.request().method() === "POST"
      && new URL(response.url()).pathname === "/api/v1/integrations/acumatica/projects/P-251-ID/tasks/sync"
    ));
    await detail.getByRole("button", { name: "Synchroniser les tâches ERP" }).click();
    const response = await syncResponse;
    expect(response.ok()).toBeTruthy();

    await expect(detail).toContainText("Budgets ERP synchronisés");

    const budgets = detail.locator(".project-task-contact-list").filter({ hasText: "Budgets tâches ERP" }).first();
    await expect(budgets.getByRole("columnheader", { name: "Budget ERP" })).toBeVisible();
    await expect(budgets.getByRole("columnheader", { name: "Actual ERP" })).toBeVisible();
    await expect(budgets.getByRole("columnheader", { name: "Restant ERP" })).toBeVisible();
    await expect(budgets.getByRole("columnheader", { name: "Budget dérivé main-d’œuvre" })).toBeVisible();
    await expect(budgets.getByRole("columnheader", { name: "Charge WorkPackages" })).toBeVisible();
    await expect(budgets.getByRole("columnheader", { name: "Solde heures structuré" })).toBeVisible();

    const taskRow = budgets.locator("tbody tr").filter({ hasText: "216" }).first();
    await expect(taskRow).toBeVisible();
    await expect(taskRow).toContainText(/1\s?000,00/);
    await expect(taskRow).toContainText(/250,00/);
    await expect(taskRow).toContainText(/750,00/);
    await expect(taskRow).toContainText("Aucun WorkPackage associé");
  } finally {
    await closeContext(context);
  }
});
