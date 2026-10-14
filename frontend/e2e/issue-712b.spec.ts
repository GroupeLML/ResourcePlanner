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

const classes750 = [
  { code: "INSTALL_EXC", label: "Installation excavation", active: true },
  { code: "INSTALL_ELEC", label: "Installation \u00e9lectrique", active: true },
  { code: "EMPTY_750", label: "Classe sans ressource", active: true },
];
const resources750 = [
  { id: "R-750-EXC-A", name: "Excavation Alpha", resource_class: "INSTALL_EXC" },
  { id: "R-750-EXC-B", name: "Excavation Beta", resource_class: "INSTALL_EXC" },
  { id: "R-750-EXC-C", name: "Excavation Gamma", resource_class: "INSTALL_EXC" },
  { id: "R-750-ELEC", name: "Installation controle", resource_class: "INSTALL_ELEC" },
];

async function project750Resources(page: Page, visible: typeof resources750) {
  // Browser contract only. SQL/API authorization and the no-schedule regression
  // are exercised without mocks in test_planning_excavation_750.py.
  await page.route("**/api/v1/planning/snapshot?**", async (route) => {
    const response = await route.fetch();
    const snapshot = await response.json();
    const template = snapshot.resources[0];
    if (!template) throw new Error("Fixture Planning 750: ressource de reference requise");
    await route.fulfill({ response, json: {
      ...snapshot,
      resources: visible.map((resource) => ({ ...template, ...resource })),
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
    if (!catalog[0]) throw new Error("Fixture Planning 750: catalogue de reference requis");
    // An editor's broader resource catalog is NOT the source for visible groups.
    await route.fulfill({ response, json: resources750.map((resource) => ({ ...catalog[0], ...resource })) });
  });
}

test("750 affiche Installation excavation et toutes ses ressources sans creer de groupes vides", async ({ browser }) => {
  const { context, page } = await openPlanning(browser, classes750);
  try {
    await project750Resources(page, resources750);
    await showPlanning(page);
    const excavation = page.locator('.resource-group[data-resource-class="INSTALL_EXC"]');
    const electrical = page.locator('.resource-group[data-resource-class="INSTALL_ELEC"]');
    await expect(excavation).toHaveCount(1);
    await expect(excavation.locator(".resource-group-title strong")).toHaveText("Installation excavation");
    await expect(excavation.locator(".resource-identity > strong")).toHaveText([
      "Excavation Alpha", "Excavation Beta", "Excavation Gamma",
    ]);
    for (const resource of resources750.filter((row) => row.resource_class === "INSTALL_EXC")) {
      await expect(excavation.locator(`.resource-identity[data-resource-id="${resource.id}"]`)).toHaveCount(1);
    }
    await expect(electrical).toHaveCount(1);
    await expect(electrical.locator(".resource-group-title strong")).toHaveText("Installation \u00e9lectrique");
    await expect(electrical.locator(".resource-identity > strong")).toHaveText(["Installation controle"]);
    await expect(page.locator('.resource-group[data-resource-class="EMPTY_750"]')).toHaveCount(0);

    await page.getByRole("button", { name: /^Filtres/ }).click();
    const filter = page.getByLabel("Classe");
    await expect(filter.locator('option[value="INSTALL_EXC"]')).toHaveText("Installation excavation");
    await expect(filter.locator('option[value="EMPTY_750"]')).toHaveCount(0);
    await filter.selectOption("INSTALL_EXC");
    await expect(excavation).toBeVisible();
    await expect(electrical).toHaveCount(0);
    await filter.selectOption("all");
    await expect(electrical).toBeVisible();
  } finally {
    await context.close();
  }
});

test("750 ne fabrique pas Installation excavation depuis les catalogues hors projection autorisee", async ({ browser }) => {
  const { context, page } = await openPlanning(browser, classes750);
  try {
    await project750Resources(page, resources750.filter((row) => row.resource_class === "INSTALL_ELEC"));
    await showPlanning(page);
    const electrical = page.locator('.resource-group[data-resource-class="INSTALL_ELEC"]');
    await expect(electrical.locator(".resource-group-title strong")).toHaveText("Installation \u00e9lectrique");
    await expect(page.locator('.resource-group[data-resource-class="INSTALL_EXC"]')).toHaveCount(0);
    await expect(page.locator('.resource-group[data-resource-class="EMPTY_750"]')).toHaveCount(0);
    await expect(page.locator('.resource-identity[data-resource-id^="R-750-EXC"]')).toHaveCount(0);
    await page.getByRole("button", { name: /^Filtres/ }).click();
    await expect(page.getByLabel("Classe").locator('option[value="INSTALL_EXC"]')).toHaveCount(0);
  } finally {
    await context.close();
  }
});
