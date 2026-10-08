import { Browser, Page, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

async function openPlanning(browser: Browser) {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "COORDINATOR" },
  });
  const page = await context.newPage();
  await page.goto("/");
  await expect(page.locator(".sidebar-footer")).toContainText("Coordonnateur E2E");
  return { context, page };
}

async function navigatePlanning(page: Page) {
  await page.locator(".main-nav").getByRole("button", { name: /Planning opérationnel/i }).click();
}

function todayIso() {
  const today = new Date();
  return new Date(today.getTime() - today.getTimezoneOffset() * 60_000)
    .toISOString().slice(0, 10);
}

test("709A — repère PM lisible au clavier et statut Shift inchangé", async ({ browser }) => {
  const { context, page } = await openPlanning(browser);
  try {
    // The E2E database seeds a project and resources, not a pre-existing shift.
    // Create a known shift in the current displayed week instead of assuming one exists.
    const createdResponse = await page.request.post("/api/v1/quick-shifts", {
      headers: { "Idempotency-Key": "709a-e2e-manager-marker" },
      data: {
        project_number: "P-251",
        technician: "Alice",
        day: todayIso(),
        hours: 1,
        description: "Repère chargé E2E 709A",
        confirmation: "Confirmée",
      },
    });
    expect(createdResponse.status(), await createdResponse.text()).toBe(201);
    const created = await createdResponse.json() as { allocation_id: string };
    expect(created.allocation_id).toBeTruthy();

    await navigatePlanning(page);
    const card = page.locator(`.shift-card[data-allocation-id="${created.allocation_id}"]`);
    await expect(card).toBeVisible();
    const marker = card.locator(".shift-pm-marker");
    await expect(marker).toContainText("Chargé : Chargé E2E");
    await expect(marker).toHaveAttribute("aria-label", /Chargé de projet :/);
    await expect(marker).toHaveAttribute("tabindex", "0");
    await marker.focus();
    await expect(marker).toBeFocused();

    const color = await marker.locator(".shift-pm-dot").evaluate(
      (dot) => getComputedStyle(dot).backgroundColor,
    );
    expect(color).not.toBe("rgb(100, 116, 139)");
    const status = await card.locator(".confirmation-badge").textContent();
    await page.reload();
    await navigatePlanning(page);

    const restored = page.locator(`.shift-card[data-allocation-id="${created.allocation_id}"]`);
    await expect(restored.locator(".shift-pm-marker")).toBeVisible();
    expect(await restored.locator(".shift-pm-dot").evaluate(
      (dot) => getComputedStyle(dot).backgroundColor,
    )).toBe(color);
    expect(await restored.locator(".confirmation-badge").textContent()).toBe(status);
  } finally {
    await context.close();
  }
});
