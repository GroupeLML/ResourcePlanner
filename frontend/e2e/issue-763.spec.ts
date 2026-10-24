import { Browser, Page, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

async function openPlanning(browser: Browser) {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "COORDINATOR" },
  });
  const page = await context.newPage();
  return { context, page };
}

async function showPlanning(page: Page) {
  await page.goto("/");
  await expect(page.locator(".sidebar-footer")).toContainText("Coordonnateur E2E");
  await page.locator(".main-nav").getByRole("button", { name: /Planning opérationnel/i }).click();
}

async function names(scope: ReturnType<Page["locator"]>) {
  return scope.locator(".resource-identity > strong").allTextContents();
}

test("763 subdivise Installation électrique à partir du sous-groupe projeté par le backend", async ({ browser }) => {
  const { context, page } = await openPlanning(browser);

  const electrical = [
    {
      id: "R-763-COMP",
      name: "E2E Compagnon",
      resource_class: "INSTALL_ELEC",
      planning_subgroup_code: "COMPANION",
      planning_subgroup_label: "Compagnons électriciens",
    },
    ...["1", "2", "3", "4"].map((year) => ({
      id: `R-763-APP${year}`,
      name: `E2E Apprenti ${year}`,
      resource_class: "INSTALL_ELEC",
      planning_subgroup_code: "APPRENTICE",
      planning_subgroup_label: "Apprentis électriciens",
    })),
    {
      id: "R-763-OTHER",
      name: "E2E Électrique non classé",
      resource_class: "INSTALL_ELEC",
      planning_subgroup_code: "OTHER",
      planning_subgroup_label: "Autres / Non classé",
    },
  ];

  try {
    await page.route("**/api/v1/resource-classes", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          { code: "INSTALL_ELEC", label: "Installation électrique", active: true },
          { code: "PROGRAMMEUR", label: "Programmation", active: true },
        ]),
      });
    });
    await page.route("**/api/v1/planning/snapshot?**", async (route) => {
      const response = await route.fetch();
      const snapshot = await response.json();
      const template = snapshot.resources[0];
      if (!template) throw new Error("763 requires an existing Planning resource template");
      await route.fulfill({
        response,
        json: {
          ...snapshot,
          resources: [
            ...electrical.map((row) => ({ ...template, ...row })),
            {
              ...template,
              id: "R-763-CONTROL",
              name: "E2E Programmeur contrôle",
              resource_class: "PROGRAMMEUR",
              planning_subgroup_code: null,
              planning_subgroup_label: null,
            },
          ],
          shifts: [],
          segments: [],
          demands: [],
          pending_loads: [],
        },
      });
    });
    await page.route("**/api/v1/planning/actions?**", (route) => route.fulfill({ json: [] }));
    await page.route("**/api/v1/planning/capacity-grid?**", async (route) => {
      const response = await route.fetch();
      await route.fulfill({
        response,
        json: { ...await response.json(), resources: [], segment_diagnostics: [] },
      });
    });

    await showPlanning(page);

    const parent = page.locator('.resource-group[data-resource-class="INSTALL_ELEC"]');
    const parentHeading = parent.locator(".resource-group-heading");
    await expect(parentHeading.locator(".resource-group-title strong")).toHaveText("Installation électrique");
    await expect(parentHeading).toHaveAttribute("aria-expanded", "true");

    const companion = parent.locator('.resource-subgroup[data-resource-subgroup="COMPANION"]');
    const apprentice = parent.locator('.resource-subgroup[data-resource-subgroup="APPRENTICE"]');
    const other = parent.locator('.resource-subgroup[data-resource-subgroup="OTHER"]');

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
    await expect(parent.locator(".resource-subgroup")).toHaveCount(0);
    await expect(parent.locator(".resource-row")).toHaveCount(0);

    await parentHeading.click();
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

    await page.getByRole("button", { name: /^Filtres/ }).click();
    const search = page.getByLabel("Recherche");
    await search.fill("E2E Apprenti 2");
    await expect(companion).toHaveCount(0);
    await expect(other).toHaveCount(0);
    await expect.poll(() => names(apprentice)).toEqual(["E2E Apprenti 2"]);
    await search.fill("");

    const programmeur = page.locator('.resource-group[data-resource-class="PROGRAMMEUR"]');
    await expect(programmeur.locator(".resource-subgroup")).toHaveCount(0);
    await expect.poll(() => names(programmeur)).toEqual(["E2E Programmeur contrôle"]);

    await expect(page.locator('.resource-group[data-resource-class="INSTALL_ELEC_COMPAGNON"]')).toHaveCount(0);
    await expect(page.locator('.resource-group[data-resource-class="INSTALL_ELEC_APPRENTI"]')).toHaveCount(0);
  } finally {
    await context.close();
  }
});
