import { Browser, Page, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

async function openRole(browser: Browser, role: "ADMIN" | "COORDINATOR", expectedName: string) {
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
  // #656 validates identity-owned ordering on identities that are actually
  // authorized for the global Planning catalog under ADR-027.
  const globalScope = page.getByRole("button", { name: "Vue globale", exact: true });
  if (await globalScope.isVisible()) {
    await globalScope.click();
  }
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
  const coordinator = await openRole(browser, "COORDINATOR", "Coordonnateur E2E");
  try {
    await expectGlobalFallback(coordinator.page);
    await navigatePlanning(coordinator.page);
    await expect.poll(() => programmerNames(coordinator.page)).toEqual(["Alice", "Bob"]);
    await coordinator.page.getByRole("button", { name: "Monter Bob", exact: true }).click();
    await expect.poll(() => programmerNames(coordinator.page)).toEqual(["Bob", "Alice"]);
  } finally {
    await coordinator.context.close();
  }

  const admin = await openRole(browser, "ADMIN", "Administrateur E2E");
  try {
    await expectGlobalFallback(admin.page);
    await navigatePlanning(admin.page);
    await expect.poll(() => programmerNames(admin.page)).toEqual(["Alice", "Bob"]);
  } finally {
    await admin.context.close();
  }

  const coordinatorReloaded = await openRole(browser, "COORDINATOR", "Coordonnateur E2E");
  try {
    await navigatePlanning(coordinatorReloaded.page);
    await expect.poll(() => programmerNames(coordinatorReloaded.page)).toEqual(["Bob", "Alice"]);
    await coordinatorReloaded.page.getByLabel("Ordre des ressources").selectOption("alphabetical");
    await expect.poll(() => programmerNames(coordinatorReloaded.page)).toEqual(["Alice", "Bob"]);
    await coordinatorReloaded.page.getByLabel("Ordre des ressources").selectOption("manual");
    await expect.poll(() => programmerNames(coordinatorReloaded.page)).toEqual(["Bob", "Alice"]);

    // Restore this user's canonical fixture order for the remaining serial E2E suite.
    await coordinatorReloaded.page.getByRole("button", { name: "Descendre Bob", exact: true }).click();
    await expect.poll(() => programmerNames(coordinatorReloaded.page)).toEqual(["Alice", "Bob"]);
  } finally {
    await coordinatorReloaded.context.close();
  }
});
