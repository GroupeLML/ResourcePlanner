import { Browser, Page, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

async function openProject(browser: Browser, role: string) {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": role },
  });
  const page = await context.newPage();
  await page.goto("/");
  await page.locator(".main-nav").getByRole("button", { name: /Projets/i }).click();
  const row = page.locator(".projects-table tbody tr").filter({ hasText: "P-251" }).first();
  await expect(row).toBeVisible();
  await row.getByRole("button", { name: "Ouvrir" }).click();
  return { context, page };
}

test("617D choisit, remplace et retire une ressource attitrée avec CAS", async ({ browser }) => {
  let chosen: string | null = null;
  let version = 1;
  const requests: Array<{ resource_id: string | null; expected_version: number }> = [];
  const context = await browser.newContext({
    baseURL: BASE_URL, locale: "fr-CA", extraHTTPHeaders: { "X-E2E-Role": "ADMIN" },
  });
  const page = await context.newPage();
  try {
    await page.route("**/api/v1/resources?**", async (route) => {
      await route.fulfill({ json: [
        { id: "R-617D-A", name: "Alice Test", active: true, erp_active: true },
        { id: "R-617D-B", name: "Bruno Test", active: true, erp_active: true },
        { id: "R-617D-X", name: "Ancien Test", active: false, erp_active: false },
      ] });
    });
    await page.route("**/api/v1/task-catalog?**", async (route) => {
      const response = await route.fetch();
      const rows = await response.json() as Array<Record<string, unknown>>;
      await route.fulfill({ response, json: rows.map((row, index) => index === 0
        ? { ...row, preferred_resource_id: chosen, preferred_resource_version: version }
        : row) });
    });
    await page.route("**/api/v1/task-catalog/*/preferred-resource", async (route) => {
      const body = route.request().postDataJSON() as { resource_id: string | null; expected_version: number };
      requests.push(body);
      if (body.expected_version !== version) {
        await route.fulfill({ status: 409, json: { error: { code: "task_preferred_resource_version_conflict", message: "Conflit" } } });
        return;
      }
      chosen = body.resource_id;
      version++;
      await route.fulfill({ json: {
        task_catalog_item_id: "TASK-617D", preferred_resource_id: chosen, version, action: "UPDATED",
      } });
    });
    await page.goto("/");
    await page.locator(".main-nav").getByRole("button", { name: /Projets/i }).click();
    const projectRow = page.locator(".projects-table tbody tr").filter({ hasText: "P-251" }).first();
    await expect(projectRow).toBeVisible();
    await projectRow.getByRole("button", { name: "Ouvrir" }).click();
    const select = page.getByRole("combobox", { name: /^Ressource attitrée / }).first();
    await expect(select).toBeVisible();
    await expect(select).toHaveValue("");
    await select.selectOption("R-617D-A");
    await expect(select).toHaveValue("R-617D-A");
    await select.selectOption("R-617D-B");
    await expect(select).toHaveValue("R-617D-B");
    await select.selectOption("");
    await expect(select).toHaveValue("");
    expect(requests).toEqual([
      { resource_id: "R-617D-A", expected_version: 1 },
      { resource_id: "R-617D-B", expected_version: 2 },
      { resource_id: null, expected_version: 3 },
    ]);
    await page.reload();
    await page.locator(".main-nav").getByRole("button", { name: /Projets/i }).click();
    const reloaded = page.locator(".projects-table tbody tr").filter({ hasText: "P-251" }).first();
    await reloaded.getByRole("button", { name: "Ouvrir" }).click();
    await expect(page.getByRole("combobox", { name: /^Ressource attitrée / }).first()).toHaveValue("");
  } finally { await context.close(); }
});

test("617D préserve une nomination inactive et recharge après conflit CAS", async ({ browser }) => {
  const context = await browser.newContext({
    baseURL: BASE_URL, locale: "fr-CA", extraHTTPHeaders: { "X-E2E-Role": "ADMIN" },
  });
  const page = await context.newPage();
  let version = 1;
  let preference = "R-617D-X";
  try {
    await page.route("**/api/v1/resources?**", (route) => route.fulfill({ json: [
      { id: "R-617D-A", name: "Alice Test", active: true, erp_active: true },
      { id: "R-617D-X", name: "Ancien Test", active: false, erp_active: false },
    ] }));
    await page.route("**/api/v1/task-catalog?**", async (route) => {
      const response = await route.fetch();
      const rows = await response.json() as Array<Record<string, unknown>>;
      await route.fulfill({ response, json: rows.map((row, index) => index === 0
        ? { ...row, preferred_resource_id: preference, preferred_resource_version: version }
        : row) });
    });
    await page.route("**/api/v1/task-catalog/*/preferred-resource", (route) => {
      preference = "R-617D-A";
      version = 2;
      return route.fulfill({ status: 409, json: { error: { code: "task_preferred_resource_version_conflict", message: "Conflit" } } });
    });
    await page.goto("/");
    await page.locator(".main-nav").getByRole("button", { name: /Projets/i }).click();
    const row = page.locator(".projects-table tbody tr").filter({ hasText: "P-251" }).first();
    await row.getByRole("button", { name: "Ouvrir" }).click();
    const select = page.getByRole("combobox", { name: /^Ressource attitrée / }).first();
    await expect(select).toHaveValue("R-617D-X");
    await expect(page.getByText(/Nomination historique inactive/).first()).toBeVisible();
    await select.selectOption("R-617D-A");
    await expect(page.getByText(/Valeur serveur rechargée/).first()).toBeVisible();
    await expect(select).toHaveValue("R-617D-A");
  } finally { await context.close(); }
});
