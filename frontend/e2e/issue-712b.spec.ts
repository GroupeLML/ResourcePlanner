import { Browser, BrowserContext, Page, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

async function openPlanning(browser: Browser, classes: Array<{ code: string; label: string; active: boolean }>) {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "COORDINATOR" },
  });
  const page = await context.newPage();
  await page.route("**/api/v1/resource-classes", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(classes) });
  });
  return { context, page };
}

async function showPlanning(page: Page) {
  await page.goto("/");
  await expect(page.locator(".sidebar-footer")).toContainText("Coordonnateur E2E");
  await page.locator(".main-nav").getByRole("button", { name: /Planning opérationnel/i }).click();
}

test("712B affiche le libellé métier d'une classe inactive, sans changer le code des groupes et filtres", async ({ browser }) => {
  const { context, page } = await openPlanning(browser, [
    { code: "PROGRAMMEUR", label: "Programmation industrielle historique", active: false },
  ]);

  try {
    await showPlanning(page);
    const group = page.locator('.resource-group[data-resource-class="PROGRAMMEUR"]').first();
    const heading = group.locator(".resource-group-heading");
    await expect(heading.locator(".resource-group-title strong")).toHaveText("Programmation industrielle historique");
    await expect(group.locator(".resource-identity > strong")).toHaveCount(2);

    await heading.click();
    await expect(heading).toHaveAttribute("aria-expanded", "false");
    await heading.click();
    await expect(heading).toHaveAttribute("aria-expanded", "true");

    await page.getByRole("button", { name: /^Filtres/ }).click();
    const classFilter = page.getByLabel("Classe");
    await expect(classFilter.locator('option[value="PROGRAMMEUR"]')).toHaveText("Programmation industrielle historique");
    await classFilter.selectOption("PROGRAMMEUR");
    await expect(group).toBeVisible();
    await expect(classFilter).toHaveValue("PROGRAMMEUR");
  } finally {
    await context.close();
  }
});

test("712B conserve les groupes distincts et Non classé si classe absente ou non résolue", async ({ browser }) => {
  const { context, page } = await openPlanning(browser, []);
  await page.route("**/api/v1/planning/snapshot?**", async (route) => {
    const response = await route.fetch();
    const snapshot = await response.json() as { resources: Array<{ resource_class: string | null }> };
    if (snapshot.resources.length < 2) throw new Error("Fixture Planning 712B: au moins deux ressources requises");
    snapshot.resources[0].resource_class = null;
    await route.fulfill({ response, contentType: "application/json", body: JSON.stringify(snapshot) });
  });

  try {
    await showPlanning(page);
    const withoutClass = page.locator('.resource-group[data-resource-class="Non classé"]');
    const unresolved = page.locator('.resource-group[data-resource-class="PROGRAMMEUR"]');
    await expect(withoutClass.locator(".resource-group-title strong")).toHaveText("Non classé");
    await expect(unresolved.locator(".resource-group-title strong")).toHaveText("Non classé");

    await page.getByRole("button", { name: /^Filtres/ }).click();
    const classFilter = page.getByLabel("Classe");
    await expect(classFilter.locator('option[value="PROGRAMMEUR"]')).toHaveText("Non classé");
    await classFilter.selectOption("PROGRAMMEUR");
    await expect(unresolved).toBeVisible();
    await expect(withoutClass).toHaveCount(0);
    await classFilter.selectOption("Non classé");
    await expect(withoutClass).toBeVisible();
    await expect(unresolved).toHaveCount(0);
  } finally {
    await context.close();
  }
});
