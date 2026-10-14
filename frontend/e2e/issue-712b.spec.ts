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

// Rendering contract only: SQL regression tests establish schedule eligibility.
// React must use the projected resource population, never rebuild that policy.
test("753 creates resource groups only from the eligible Planning projection", async ({ browser }) => {
  const classes = [
    { code: "INSTALL_ELEC", label: "Installation électrique", active: true },
    { code: "INSTALL_EXC", label: "Installation excavation", active: true },
    { code: "EMPTY_753", label: "Empty class", active: true },
  ];
  const members = [
    { id: "R-753-E1", name: "Electrical Alpha", resource_class: "INSTALL_ELEC" },
    { id: "R-753-E2", name: "Electrical Beta", resource_class: "INSTALL_ELEC" },
    { id: "R-753-E3", name: "Electrical Gamma", resource_class: "INSTALL_ELEC" },
    { id: "R-753-X1", name: "Excavation Alpha", resource_class: "INSTALL_EXC" },
  ];
  let additionalSchedulesConfigured = false;
  const { context, page } = await openPlanning(browser, classes);
  try {
    await page.route("**/api/v1/planning/snapshot?**", async (route) => {
      const response = await route.fetch();
      const snapshot = await response.json();
      const template = snapshot.resources[0];
      if (!template) throw new Error("753 requires the existing scheduled resource fixture");
      const eligible = additionalSchedulesConfigured ? members : members.slice(0, 2);
      await route.fulfill({ response, json: {
        ...snapshot,
        resources: eligible.map((row) => ({ ...template, ...row })),
        shifts: [], segments: [], demands: [], pending_loads: [],
      } });
    });
    await page.route("**/api/v1/planning/actions?**", (route) => route.fulfill({ json: [] }));
    await page.route("**/api/v1/planning/capacity-grid?**", async (route) => {
      const response = await route.fetch();
      await route.fulfill({ response, json: {
        ...await response.json(), resources: [], segment_diagnostics: [],
      } });
    });
    await page.route("**/api/v1/resources?**", async (route) => {
      const response = await route.fetch();
      const catalog = await response.json();
      if (!catalog[0]) throw new Error("753 requires the existing resource catalog fixture");
      // Active/classified editor entries cannot create a Planning resource row.
      await route.fulfill({ response, json: members.map((row) => ({ ...catalog[0], ...row })) });
    });

    await showPlanning(page);
    const electrical = page.locator('.resource-group[data-resource-class="INSTALL_ELEC"]');
    const excavation = page.locator('.resource-group[data-resource-class="INSTALL_EXC"]');
    await expect(electrical).toHaveCount(1);
    await expect(electrical.locator(".resource-identity > strong")).toHaveCount(2);
    await expect(excavation).toHaveCount(0);
    await expect(page.locator('.resource-group[data-resource-class="EMPTY_753"]')).toHaveCount(0);
    await page.getByRole("button", { name: /^Filtres/ }).click();
    await expect(page.getByLabel("Classe").locator('option[value="INSTALL_EXC"]')).toHaveCount(0);

    additionalSchedulesConfigured = true;
    await showPlanning(page);
    await expect(electrical).toHaveCount(1);
    await expect(electrical.locator(".resource-identity > strong")).toHaveCount(3);
    await expect(excavation).toHaveCount(1);
    await expect(excavation.locator(".resource-group-title strong")).toHaveText("Installation excavation");
    await expect(excavation.locator(".resource-identity > strong")).toHaveText("Excavation Alpha");
    await expect(page.locator('.resource-group[data-resource-class="EMPTY_753"]')).toHaveCount(0);
  } finally {
    await context.close();
  }
});
