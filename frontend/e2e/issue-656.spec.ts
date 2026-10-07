import { Browser, Page, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

async function openRole(browser: Browser, role: "ADMIN" | "PROJECT_MANAGER", expectedName: string) {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": role },
  });
  const page = await context.newPage();
  await page.goto("/");
  await expect(page.locator(".sidebar-footer")).toContainText(expectedName);
  return { context, page };
}

async function navigatePlanning(page: Page) {
  await page.locator(".main-nav").getByRole("button", { name: /Planning opérationnel/i }).click();
  await expect(page.getByLabel("Ordre des ressources")).toHaveValue("manual");
}

async function programmerNames(page: Page) {
  const group = page.locator(".resource-group").filter({ hasText: "PROGRAMMEUR" }).first();
  await expect(group).toBeVisible();
  const names = await group.locator(".resource-identity > strong").allTextContents();
  return names.filter((name) => name === "Alice" || name === "Bob");
}

async function expectGlobalFallback(page: Page) {
  const resourcesResponse = await page.request.get("/api/v1/resources?active_only=true");
  expect(resourcesResponse.status(), await resourcesResponse.text()).toBe(200);
  const resources = await resourcesResponse.json() as Array<{
    id: string;
    name: string;
    sort_order: number;
  }>;
  const alice = resources.find((resource) => resource.name === "Alice");
  const bob = resources.find((resource) => resource.name === "Bob");
  if (!alice || !bob) throw new Error("Ressources E2E Alice/Bob introuvables");

  const orderResponse = await page.request.get("/api/v1/planning/resource-order");
  expect(orderResponse.status(), await orderResponse.text()).toBe(200);
  const order = await orderResponse.json() as { positions: Record<string, number> };
  expect(order.positions[alice.id]).toBe(alice.sort_order);
  expect(order.positions[bob.id]).toBe(bob.sort_order);
  expect(order.positions[alice.id]).toBeLessThan(order.positions[bob.id]);
}

test("ordre Manuel des ressources est propre à chaque AppUser et survit à une nouvelle session", async ({ browser }) => {
  test.setTimeout(90_000);
  const projectManager = await openRole(browser, "PROJECT_MANAGER", "Chargé E2E");
  try {
    await expectGlobalFallback(projectManager.page);
    await navigatePlanning(projectManager.page);
    await expect.poll(() => programmerNames(projectManager.page)).toEqual(["Alice", "Bob"]);
    await projectManager.page.getByRole("button", { name: "Monter Bob", exact: true }).click();
    await expect.poll(() => programmerNames(projectManager.page)).toEqual(["Bob", "Alice"]);
  } finally {
    await projectManager.context.close();
  }

  const admin = await openRole(browser, "ADMIN", "Administrateur E2E");
  try {
    await expectGlobalFallback(admin.page);
    await navigatePlanning(admin.page);
    await expect.poll(() => programmerNames(admin.page)).toEqual(["Alice", "Bob"]);
  } finally {
    await admin.context.close();
  }

  const projectManagerReloaded = await openRole(browser, "PROJECT_MANAGER", "Chargé E2E");
  try {
    await navigatePlanning(projectManagerReloaded.page);
    await expect.poll(() => programmerNames(projectManagerReloaded.page)).toEqual(["Bob", "Alice"]);
    await projectManagerReloaded.page.getByLabel("Ordre des ressources").selectOption("alphabetical");
    await expect.poll(() => programmerNames(projectManagerReloaded.page)).toEqual(["Alice", "Bob"]);
    await projectManagerReloaded.page.getByLabel("Ordre des ressources").selectOption("manual");
    await expect.poll(() => programmerNames(projectManagerReloaded.page)).toEqual(["Bob", "Alice"]);

    // Restore this user's canonical fixture order for the remaining serial E2E suite.
    await projectManagerReloaded.page.getByRole("button", { name: "Descendre Bob", exact: true }).click();
    await expect.poll(() => programmerNames(projectManagerReloaded.page)).toEqual(["Alice", "Bob"]);
  } finally {
    await projectManagerReloaded.context.close();
  }
});
