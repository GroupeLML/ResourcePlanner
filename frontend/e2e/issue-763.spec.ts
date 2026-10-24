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
  await page.locator(".main-nav").getByRole("button", { name: /Planning opérationnel/i }).click();
  return { context, page };
}

async function names(scope: ReturnType<Page["locator"]>) {
  return scope.locator(".resource-identity > strong").allTextContents();
}

test("763 subdivise Installation électrique selon le EmployeeClass ERP projeté", async ({ browser }) => {
  const { context, page } = await openCoordinator(browser);

  try {
    const electrical = page.locator('.resource-group[data-resource-class="INSTALL_ELEC"]');
    const parentHeading = electrical.locator(".resource-group-heading");
    await expect(parentHeading.locator(".resource-group-title strong")).toHaveText("Installation électrique");
    await expect(parentHeading).toHaveAttribute("aria-expanded", "true");

    const companion = electrical.locator('.resource-subgroup[data-resource-subgroup="COMPANION"]');
    const apprentice = electrical.locator('.resource-subgroup[data-resource-subgroup="APPRENTICE"]');
    const other = electrical.locator('.resource-subgroup[data-resource-subgroup="OTHER"]');

    await expect(companion.locator(".resource-subgroup-heading strong")).toHaveText("Compagnons électriciens");
    await expect(apprentice.locator(".resource-subgroup-heading strong")).toHaveText("Apprentis électriciens");
    await expect(other.locator(".resource-subgroup-heading strong")).toHaveText("Autres / Non classé");

    await expect.poll(() => names(companion)).toEqual(["E2E Compagnon"]);
    await expect.poll(() => names(apprentice)).toEqual([
      "E2E Apprenti 1",
      "E2E Apprenti 2",
      "E2E Apprenti 3",
      "E2E Apprenti 4",
    ]);
    await expect.poll(() => names(other)).toEqual(["E2E Électrique non classé"]);

    await parentHeading.click();
    await expect(parentHeading).toHaveAttribute("aria-expanded", "false");
    await expect(electrical.locator(".resource-subgroup")).toHaveCount(0);
    await expect(electrical.locator(".resource-row")).toHaveCount(0);

    await parentHeading.click();
    await expect(parentHeading).toHaveAttribute("aria-expanded", "true");
    await expect(companion).toBeVisible();
    await expect(apprentice).toBeVisible();

    const companionHeading = companion.locator(".resource-subgroup-heading");
    const apprenticeHeading = apprentice.locator(".resource-subgroup-heading");

    await companionHeading.click();
    await expect(companionHeading).toHaveAttribute("aria-expanded", "false");
    await expect(companion.locator(".resource-row")).toHaveCount(0);
    await expect(apprenticeHeading).toHaveAttribute("aria-expanded", "true");
    await expect.poll(() => names(apprentice)).toEqual([
      "E2E Apprenti 1",
      "E2E Apprenti 2",
      "E2E Apprenti 3",
      "E2E Apprenti 4",
    ]);

    await apprenticeHeading.click();
    await expect(apprenticeHeading).toHaveAttribute("aria-expanded", "false");
    await companionHeading.click();
    await expect(companionHeading).toHaveAttribute("aria-expanded", "true");
    await expect.poll(() => names(companion)).toEqual(["E2E Compagnon"]);

    await apprenticeHeading.click();
    await expect(apprenticeHeading).toHaveAttribute("aria-expanded", "true");

    // Filters operate on the already-authorized Planning population before subgroup rendering.
    await page.getByRole("button", { name: /^Filtres/ }).click();
    const search = page.getByLabel("Recherche");
    await search.fill("E2E Apprenti 2");
    await expect(companion).toHaveCount(0);
    await expect(other).toHaveCount(0);
    await expect.poll(() => names(apprentice)).toEqual(["E2E Apprenti 2"]);
    await search.fill("");

    // Unrelated resource classes retain their existing flat behavior.
    const programmeur = page.locator('.resource-group[data-resource-class="PROGRAMMEUR"]');
    await expect(programmeur.locator(".resource-subgroup")).toHaveCount(0);
    await expect.poll(() => names(programmeur)).toEqual(["Alice", "Bob"]);

    await expect(page.locator('.resource-group[data-resource-class="INSTALL_ELEC_COMPAGNON"]')).toHaveCount(0);
    await expect(page.locator('.resource-group[data-resource-class="INSTALL_ELEC_APPRENTI"]')).toHaveCount(0);
  } finally {
    await context.close();
  }
});
