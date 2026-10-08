import { Browser, BrowserContext, Page, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

type Recommendation = {
  resource_id: string;
  resource_name: string;
  resource_class: string | null;
  required_competency: string | null;
  required_class: string | null;
  competency_match: boolean;
  class_match: boolean;
  capacity_hours: number;
  confirmed_hours: number;
  tentative_hours: number;
  outside_standard_hours: number;
  free_after_confirmed: number;
  prudent_free: number;
  overtime_needed: number;
  enough_after_confirmed: boolean;
  enough_prudent: boolean;
  score: number;
  rank: number;
  recommended: boolean;
  preferred: boolean;
  recommendation_category: number;
  competency_state: "NOT_REQUIRED" | "SATISFIED" | "MISSING" | "UNRESOLVED";
  missing_competency_ids: string[];
  capacity_state: "PRUDENT_FULL" | "PRUDENT_PARTIAL" | "TENTATIVE_ONLY" | "NONE";
  fallback_requires_confirmation: boolean;
  preferred_resource_id: string | null;
  preferred_resource_name: string | null;
  preferred_resource_status:
    | "NONE"
    | "ELIGIBLE"
    | "INACTIVE_LOCAL"
    | "INACTIVE_ERP"
    | "NO_SCHEDULE_IN_WINDOW"
    | "NOT_FOUND"
    | "INVALID_TASK_CONTEXT"
    | "UNRESOLVED_CONTEXT";
};

const action = {
  kind: "ASSIGNMENT",
  reference: "REQ-617C",
  demand_number: "DMO-617C",
  segment_id: "SEG-617C",
  project_number: "P-617C",
  project_name: "Projet recommandation",
  task_code: "310",
  task_label: "Programmation",
  start_date: "2026-10-05",
  end_date: "2026-10-09",
  planned_hours: 8,
  required_competency: "PLC; SCADA",
  required_competency_id: "C-PLC",
  priority: "Normale",
  status: "À assigner",
  confirmation: "Confirmée",
  project_manager: "CP 617C",
  requester: "Demandeur 617C",
  emergency_override_active: false,
};

function recommendation(
  resourceId: string,
  resourceName: string,
  overrides: Partial<Recommendation> = {},
): Recommendation {
  return {
    resource_id: resourceId,
    resource_name: resourceName,
    resource_class: "PROGRAMMEUR",
    required_competency: "PLC; SCADA",
    required_class: "PROGRAMMEUR",
    competency_match: true,
    class_match: true,
    capacity_hours: 40,
    confirmed_hours: 8,
    tentative_hours: 0,
    outside_standard_hours: 0,
    free_after_confirmed: 32,
    prudent_free: 32,
    overtime_needed: 0,
    enough_after_confirmed: true,
    enough_prudent: true,
    score: 100,
    rank: 1,
    recommended: false,
    preferred: false,
    recommendation_category: 2,
    competency_state: "SATISFIED",
    missing_competency_ids: [],
    capacity_state: "PRUDENT_FULL",
    fallback_requires_confirmation: false,
    preferred_resource_id: null,
    preferred_resource_name: null,
    preferred_resource_status: "NONE",
    ...overrides,
  };
}

async function openCoordinator(
  browser: Browser,
  recommendations: Recommendation[],
) {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "COORDINATOR" },
  });
  const page = await context.newPage();
  let assignmentPosts = 0;
  let assignedResourceId: string | null = null;

  await page.route("**/api/v1/planning/actions**", async (route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(assignedResourceId ? [] : [action]),
    });
  });
  await page.route("**/api/v1/segments/SEG-617C/resource-recommendations", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(recommendations),
    });
  });
  await page.route("**/api/v1/segments/SEG-617C/assign", async (route) => {
    if (route.request().method() !== "POST") {
      await route.continue();
      return;
    }
    assignmentPosts += 1;
    const body = route.request().postDataJSON() as { resource_id?: string };
    assignedResourceId = body.resource_id ?? null;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        segment_id: "SEG-617C",
        action: "assigned",
        technician: null,
      }),
    });
  });

  await page.goto("/");
  await expect(page.locator(".sidebar-footer")).toContainText("Coordonnateur E2E");
  return {
    context,
    page,
    assignmentPosts: () => assignmentPosts,
    assignedResourceId: () => assignedResourceId,
  };
}

async function openRecommendations(page: Page) {
  const actionCard = page.locator(".planning-action-card").filter({ hasText: "P-617C" });
  await expect(actionCard).toBeVisible();
  await actionCard.getByRole("button", { name: "Trouver une ressource" }).click();
  const dialog = page.getByRole("dialog", { name: "Trouver une ressource" });
  await expect(dialog).toBeVisible();
  return dialog;
}

async function closeContext(context: BrowserContext) {
  await context.close();
}

test("617C conserve l'ordre backend et explique attitré, rang et diagnostics sans assigner", async ({ browser }) => {
  const recommendations = [
    recommendation("R-ZOE", "Zoé complète", {
      rank: 1,
      recommended: true,
      recommendation_category: 2,
      preferred_resource_id: "R-ALICE",
      preferred_resource_name: "Alice attitrée",
      preferred_resource_status: "ELIGIBLE",
    }),
    recommendation("R-ALICE", "Alice attitrée", {
      rank: 2,
      preferred: true,
      competency_match: false,
      competency_state: "MISSING",
      missing_competency_ids: ["C-SCADA"],
      recommendation_category: 3,
      preferred_resource_id: "R-ALICE",
      preferred_resource_name: "Alice attitrée",
      preferred_resource_status: "ELIGIBLE",
    }),
    recommendation("R-MARC", "Marc capacité partielle", {
      rank: 3,
      prudent_free: 4,
      free_after_confirmed: 4,
      enough_after_confirmed: false,
      enough_prudent: false,
      recommendation_category: 4,
      capacity_state: "PRUDENT_PARTIAL",
      preferred_resource_id: "R-ALICE",
      preferred_resource_name: "Alice attitrée",
      preferred_resource_status: "ELIGIBLE",
    }),
  ];
  const opened = await openCoordinator(browser, recommendations);

  try {
    const dialog = await openRecommendations(opened.page);
    await expect(dialog.getByTestId("preferred-resource-context")).toContainText("Alice attitrée");

    const names = await dialog.locator(".recommendation-card-heading > div > strong").allTextContents();
    expect(names).toEqual(["Zoé complète", "Alice attitrée", "Marc capacité partielle"]);

    const cards = dialog.locator(".recommendation-card");
    await expect(cards.nth(0)).toContainText("Recommandé");
    await expect(cards.nth(0)).toContainText("Catégorie 2");
    await expect(cards.nth(1)).toContainText("Attitré");
    await expect(cards.nth(1)).toContainText("Compétences manquantes : C-SCADA");
    await expect(cards.nth(2)).toContainText("Capacité prudente partielle");
    expect(opened.assignmentPosts()).toBe(0);
  } finally {
    await closeContext(opened.context);
  }
});

test("617C exige une confirmation explicite avant un repli à compétences incomplètes", async ({ browser }) => {
  const recommendations = [
    recommendation("R-BOB", "Bob repli", {
      rank: 1,
      recommended: false,
      competency_match: false,
      competency_state: "MISSING",
      missing_competency_ids: ["C-SCADA"],
      recommendation_category: 3,
      fallback_requires_confirmation: true,
      preferred_resource_id: "R-ALICE",
      preferred_resource_name: "Alice attitrée",
      preferred_resource_status: "INACTIVE_LOCAL",
    }),
  ];
  const opened = await openCoordinator(browser, recommendations);

  try {
    const dialog = await openRecommendations(opened.page);
    await expect(dialog.getByTestId("preferred-resource-context")).toContainText("inactif");
    const fallback = dialog.locator(".recommendation-card").first();
    await expect(fallback).toContainText("Repli à confirmer");
    await expect(fallback).toContainText("confirmation explicite");

    const fallbackButton = fallback.getByRole("button", { name: "Confirmer ce repli" });
    // Subscribe before the click: a native confirm pauses the page until handled.
    const rejectedDialog = opened.page.waitForEvent("dialog", { timeout: 10_000 });
    const rejectedClick = fallbackButton.click();
    const rejection = await rejectedDialog;
    expect(rejection.type()).toBe("confirm");
    expect(rejection.message()).toContain("C-SCADA");
    await rejection.dismiss();
    await rejectedClick;
    await expect.poll(opened.assignmentPosts).toBe(0);

    const assigned = opened.page.waitForResponse((response) => (
      response.request().method() === "POST"
      && new URL(response.url()).pathname === "/api/v1/segments/SEG-617C/assign"
    ), { timeout: 15_000 });
    const acceptedDialog = opened.page.waitForEvent("dialog", { timeout: 10_000 });
    const acceptedClick = fallbackButton.click();
    const acceptance = await acceptedDialog;
    expect(acceptance.type()).toBe("confirm");
    expect(acceptance.message()).toContain("C-SCADA");
    await acceptance.accept();
    await acceptedClick;
    expect((await assigned).status()).toBe(200);
    await expect.poll(opened.assignmentPosts).toBe(1);
    expect(opened.assignedResourceId()).toBe("R-BOB");
    await expect(opened.page.getByRole("dialog", { name: "Trouver une ressource" })).toBeHidden();
  } finally {
    await closeContext(opened.context);
  }
});
