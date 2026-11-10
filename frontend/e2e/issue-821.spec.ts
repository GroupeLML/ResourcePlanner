import { Browser, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";
const BUILT_REVISION = "0123456789abcdef0123456789abcdef01234567";

async function openAdmin(browser: Browser, buildMeta: string) {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "ADMIN" },
  });
  await context.route("**/build-meta.js", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/javascript",
      body: `window.RESOURCEPLANNER_BUILD = Object.freeze({ revision: "${buildMeta}" });\n`,
    });
  });
  const page = await context.newPage();
  await page.goto("/");
  await expect(page.locator(".sidebar-footer")).toContainText("Administrateur");
  return { context, page };
}

test("821 expose la révision Git complète construite au survol du shell", async ({ browser }) => {
  const { context, page } = await openAdmin(browser, BUILT_REVISION);
  try {
    const shellLabel = page.getByText("RessourcePlanner V2", { exact: true });
    await expect(shellLabel).toBeVisible();
    await shellLabel.hover();
    await expect(shellLabel).toHaveAttribute(
      "title",
      `Révision déployée : ${BUILT_REVISION}`,
    );
    await expect(shellLabel).toHaveAttribute(
      "aria-label",
      `RessourcePlanner V2 — Révision déployée : ${BUILT_REVISION}`,
    );
  } finally {
    await context.close();
  }
});

test("821 affiche un état explicite quand la métadonnée de build est invalide", async ({ browser }) => {
  const { context, page } = await openAdmin(browser, "main");
  try {
    const shellLabel = page.getByText("RessourcePlanner V2", { exact: true });
    await expect(shellLabel).toBeVisible();
    await shellLabel.hover();
    await expect(shellLabel).toHaveAttribute(
      "title",
      "Révision déployée : indisponible",
    );
    await expect(shellLabel).toHaveAttribute(
      "aria-label",
      "RessourcePlanner V2 — Révision déployée : indisponible",
    );
  } finally {
    await context.close();
  }
});
