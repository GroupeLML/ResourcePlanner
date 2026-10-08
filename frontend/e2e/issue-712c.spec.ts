import { Browser, Page, expect, test } from "@playwright/test";
import { compareResourcesByDisplayName, resourceDisplayName } from "../src/resourceLabels";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

test("712C trie sur le nom affiché, ignore le préfixe et départage les homonymes par ID stable", () => {
  const resources = [
    { id: "bob", name: "10 - Bob" },
    { id: "emile", name: "2 — Émile" },
    { id: "alice", name: "900 – Alice" },
    { id: "homonyme-z", name: "999 - Alice" },
    { id: "homonyme-a", name: "Alice" },
  ];
  expect([...resources].sort(compareResourcesByDisplayName).map((row) => row.id)).toEqual([
    "alice", "homonyme-a", "homonyme-z", "bob", "emile",
  ]);
  expect(resources[0].name).toBe("10 - Bob");
  expect(resourceDisplayName("EMP-123 - Bob")).toBe("EMP-123 - Bob");
});

async function openRole(browser: Browser, role: "ADMIN" | "COORDINATOR") {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": role },
  });
  const page = await context.newPage();
  return { context, page };
}

async function programmerNames(page: Page) {
  return page.locator('.resource-group[data-resource-class="PROGRAMMEUR"] .resource-identity > strong').allTextContents();
}

test("712C Planning Alphabétique ignore les numéros et conserve le choix Manuel", async ({ browser }) => {
  const { context, page } = await openRole(browser, "COORDINATOR");
  await page.route("**/api/v1/planning/snapshot?**", async (route) => {
    const response = await route.fetch();
    const snapshot = await response.json() as { resources: Array<{ name: string }> };
    const alice = snapshot.resources.find((row) => row.name === "Alice");
    const bob = snapshot.resources.find((row) => row.name === "Bob");
    if (!alice || !bob) throw new Error("Fixture 712C : ressources Alice et Bob requises");
    alice.name = "900 - Alice";
    bob.name = "10 - Bob";
    await route.fulfill({ response, contentType: "application/json", body: JSON.stringify(snapshot) });
  });

  try {
    await page.goto("/");
    await expect(page.locator(".sidebar-footer")).toContainText("Coordonnateur E2E");
    await page.locator(".main-nav").getByRole("button", { name: /Planning opérationnel/i }).click();
    await page.getByRole("button", { name: /^Filtres/ }).click();
    const sort = page.getByLabel("Ordre des ressources");
    await sort.selectOption("manual");
    await expect.poll(() => programmerNames(page)).toHaveLength(2);
    const manualNames = await programmerNames(page);
    await sort.selectOption("alphabetical");
    await expect.poll(() => programmerNames(page)).toEqual(["Alice", "Bob"]);
    await sort.selectOption("manual");
    await expect.poll(() => programmerNames(page)).toEqual(manualNames);
  } finally {
    await context.close();
  }
});

test("712C administration Ressources affiche une liste nominale plutôt que numérotée", async ({ browser }) => {
  const { context, page } = await openRole(browser, "ADMIN");
  await page.route("**/api/v1/resources?**", async (route) => {
    const response = await route.fetch();
    const resources = await response.json() as Array<{ name: string }>;
    const alice = resources.find((row) => row.name === "Alice");
    const bob = resources.find((row) => row.name === "Bob");
    if (!alice || !bob) throw new Error("Fixture 712C : ressources Alice et Bob requises");
    alice.name = "900 - Alice";
    bob.name = "10 - Bob";
    await route.fulfill({ response, contentType: "application/json", body: JSON.stringify(resources) });
  });

  try {
    await page.goto("/");
    await expect(page.locator(".sidebar-footer")).toContainText("Administrateur E2E");
    await page.locator(".main-nav").getByRole("button", { name: /Ressources/i }).click();
    const names = page.locator(".resource-list-item .resource-list-main strong");
    await expect.poll(async () => {
      const visible = await names.allTextContents();
      return visible.filter((name) => name === "Alice" || name === "Bob");
    }).toEqual(["Alice", "Bob"]);
  } finally {
    await context.close();
  }
});
