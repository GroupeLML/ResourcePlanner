import { Browser, Page, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

async function openAdmin(browser: Browser) {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "ADMIN" },
  });
  const page = await context.newPage();
  await page.goto("/");
  await expect(page.locator(".sidebar-footer")).toContainText("Administrateur");
  await page.locator(".main-nav").getByRole("button", { name: /Ressources/i }).click();
  return { context, page };
}

async function selectResource(page: Page, name: string) {
  const row = page.locator(".resource-list-item").filter({ hasText: name });
  await expect(row).toBeVisible();
  await row.click();
  await expect(page.locator(".resource-profile-card")).toBeVisible();
}

test("742 conserve et permet de vider la ville de résidence canonique", async ({ browser }) => {
  const { context, page } = await openAdmin(browser);
  const stamp = Date.now();
  const name = `Ressource E2E 742 ${stamp}`;

  try {
    const created = await page.request.post("/api/v1/resources", {
      headers: { "Idempotency-Key": `issue-742-${stamp}` },
      data: {
        name,
        residence_city: "Saint-Jean-sur-Richelieu",
        active: true,
        sort_order: 0,
      },
    });
    expect(created.status(), await created.text()).toBe(201);

    await page.reload();
    await expect(page.locator(".sidebar-footer")).toContainText("Administrateur");
    await page.locator(".main-nav").getByRole("button", { name: /Ressources/i }).click();
    await selectResource(page, name);

    const city = page.getByLabel("Ville de résidence");
    await expect(city).toHaveValue("Saint-Jean-sur-Richelieu");
    await city.fill("Chambly");
    const updated = page.waitForResponse(
      (response) => response.request().method() === "PATCH"
        && response.url().includes("/api/v1/resources/"),
    );
    await page.getByRole("button", { name: "Enregistrer le profil" }).click();
    expect((await updated).ok()).toBeTruthy();

    await page.reload();
    await expect(page.locator(".sidebar-footer")).toContainText("Administrateur");
    await page.locator(".main-nav").getByRole("button", { name: /Ressources/i }).click();
    await selectResource(page, name);
    await expect(page.getByLabel("Ville de résidence")).toHaveValue("Chambly");

    await page.getByLabel("Ville de résidence").fill("");
    const cleared = page.waitForResponse(
      (response) => response.request().method() === "PATCH"
        && response.url().includes("/api/v1/resources/"),
    );
    await page.getByRole("button", { name: "Enregistrer le profil" }).click();
    expect((await cleared).ok()).toBeTruthy();

    await page.reload();
    await expect(page.locator(".sidebar-footer")).toContainText("Administrateur");
    await page.locator(".main-nav").getByRole("button", { name: /Ressources/i }).click();
    await selectResource(page, name);
    await expect(page.getByLabel("Ville de résidence")).toHaveValue("");

    const resources = await page.request.get("/api/v1/resources?active_only=false");
    expect(resources.ok(), await resources.text()).toBeTruthy();
    const rows = await resources.json() as Array<{ name: string; residence_city: string | null }>;
    const row = rows.find((item) => item.name === name);
    expect(row).toBeTruthy();
    expect(row?.residence_city).toBeNull();
  } finally {
    const resources = await page.request.get("/api/v1/resources?active_only=false");
    if (resources.ok()) {
      const rows = await resources.json() as Array<{ id: string; name: string }>;
      const row = rows.find((item) => item.name === name);
      if (row) {
        await page.request.post(`/api/v1/resources/${encodeURIComponent(row.id)}/deactivate`);
      }
    }
    await context.close();
  }
});
