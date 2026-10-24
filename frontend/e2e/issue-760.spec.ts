import { Browser, Page, expect, test } from "@playwright/test";

const BASE_URL = process.env.RESOURCEPLANNER_E2E_BASE_URL || "http://127.0.0.1:8765";

async function openCoordinator(browser: Browser) {
  const context = await browser.newContext({
    baseURL: BASE_URL,
    locale: "fr-CA",
    extraHTTPHeaders: { "X-E2E-Role": "COORDINATOR" },
  });
  const page = await context.newPage();
  return { context, page };
}

function authority(
  status: "AUTHORIZED" | "NOT_AUTHORIZED" | "AMBIGUOUS_SCOPE" | "NO_ACTIVE_SCOPE",
  message: string,
) {
  return {
    allowed: status === "AUTHORIZED",
    can_assign: status === "AUTHORIZED",
    status,
    code: status === "AUTHORIZED" ? null : "test-760",
    message,
    diagnostics: [],
  };
}

test("760 distingue disponibilité, qualification et autorité avant soumission", async ({ browser }) => {
  const { context, page } = await openCoordinator(browser);
  let submitted = false;

  try {
    await page.route("**/api/v1/planning/snapshot?**", async (route) => {
      const response = await route.fetch();
      const snapshot = await response.json();
      const resource = snapshot.resources[0];
      if (!resource) throw new Error("760 requires one existing Planning resource");
      const url = new URL(route.request().url());
      const workDate = url.searchParams.get("start");
      if (!workDate) throw new Error("760 requires Planning start date");
      await route.fulfill({
        response,
        json: {
          ...snapshot,
          shifts: [{
            allocation_id: "ALLOC-760-E2E",
            segment_id: "SEG-760-E2E",
            resource_id: resource.id,
            resource_name: resource.name,
            work_date: workDate,
            hours: 8,
            allocation_type: null,
            source: "MANUAL",
            locked: true,
            outside_standard_hours: false,
            confirmation: "Confirmée",
            confirmation_override: null,
            load_kind: "FIRM",
            note: null,
            requirement_id: "REQ-760-E2E",
            demand_id: null,
            demand_number: null,
            project_id: "P-760-E2E",
            project_number: "P-760",
            project_name: "Projet autorité actifs",
            project_manager: null,
            project_manager_color_id: null,
            project_manager_color_label: null,
            requester: null,
            emergency_override_active: false,
            segment_planned_hours: 8,
            segment_locked_hours: 8,
            segment_overallocated_hours: 0,
            asset_assignment: null,
            related_asset_reservations: [],
            asset_actions: {
              assign: { allowed: true, reason: null },
              change: { allowed: false, reason: "asset_assignment_missing" },
              release: { allowed: false, reason: "asset_assignment_missing" },
            },
            asset_diagnostics: [],
          }],
          pending_loads: [],
        },
      });
    });

    await page.route("**/api/v1/assets/shifts/ALLOC-760-E2E/assignment/candidates", async (route) => {
      const base = {
        asset_type_id: "TYPE-760",
        asset_type_code: "COE",
        asset_type_label: "Camion outillé électrique",
        active: true,
        compatible: true,
        available: true,
        qualification_state: "SATISFIED",
        allowed: true,
        reason: null,
        diagnostics: [],
        currently_assigned: false,
        selection_mode: "ASSIGN",
        existing_allocation_id: null,
        existing_requirement_id: null,
        existing_operator_resource_id: null,
        existing_start_date: null,
        existing_end_date: null,
      };
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          shift_id: "ALLOC-760-E2E",
          work_date: "2026-10-24",
          operator_resource_id: "R-760",
          current_requirement_id: null,
          current_allocation_id: null,
          planning_version: 760,
          candidates: [
            {
              ...base,
              id: "ASSET-760-A",
              code: "760-A",
              label: "Autorisé",
              assignment_authority: authority("AUTHORIZED", "Autorisé à attribuer."),
            },
            {
              ...base,
              id: "ASSET-760-B",
              code: "760-B",
              label: "Non autorisé",
              assignment_authority: authority(
                "NOT_AUTHORIZED",
                "Admissible opérationnellement, mais vous n’êtes pas autorisé à attribuer cette unité.",
              ),
            },
            {
              ...base,
              id: "ASSET-760-C",
              code: "760-C",
              label: "Ambigu",
              assignment_authority: authority(
                "AMBIGUOUS_SCOPE",
                "Type d’actif associé à plusieurs périmètres d’approbation.",
              ),
            },
            {
              ...base,
              id: "ASSET-760-D",
              code: "760-D",
              label: "Sans périmètre",
              assignment_authority: authority(
                "NO_ACTIVE_SCOPE",
                "Aucun périmètre d’approbation actif valide pour ce type d’actif.",
              ),
            },
          ],
        }),
      });
    });

    await page.route("**/api/v1/assets/shifts/ALLOC-760-E2E/assignment", async (route) => {
      if (route.request().method() !== "PUT") return route.continue();
      submitted = true;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          operation: "ASSIGN",
          shift_id: "ALLOC-760-E2E",
          requirement_id: "AREQ-760",
          requirement_origin: "SHIFT_AD_HOC",
          allocation_id: "AALLOC-760",
          asset_id: "ASSET-760-A",
          operator_resource_id: "R-760",
          qualification_state: "SATISFIED",
          shift_source: "MANUAL",
          shift_locked: true,
          planning_version: 761,
        }),
      });
    });

    await page.goto("/");
    await expect(page.locator(".sidebar-footer")).toContainText("Coordonnateur");
    await page.locator(".main-nav").getByRole("button", { name: /Planning opérationnel/i }).click();

    const shift = page.locator('.shift-card[data-allocation-id="ALLOC-760-E2E"]');
    await expect(shift).toBeVisible();
    await shift.getByRole("button", { name: /Assigner un actif/ }).click();

    const dialog = page.getByRole("dialog", { name: "Assigner un actif" });
    const combobox = dialog.getByRole("combobox", { name: "Actif" });
    await combobox.click();

    const listbox = dialog.getByRole("listbox", { name: "Actif options" });
    await expect(listbox).toContainText("Disponible");
    await expect(listbox).toContainText("Conforme");
    await expect(listbox).toContainText("Autorisé à attribuer.");
    await expect(listbox).toContainText("vous n’êtes pas autorisé à attribuer cette unité");
    await expect(listbox).toContainText("Type d’actif associé à plusieurs périmètres d’approbation");
    await expect(listbox).toContainText("Aucun périmètre d’approbation actif valide");

    await listbox.getByRole("option", { name: /760-A/ }).click();
    const detail = dialog.getByTestId("shift-asset-candidate-detail");
    await expect(detail).toContainText("Disponibilité");
    await expect(detail).toContainText("Disponible");
    await expect(detail).toContainText("Qualification");
    await expect(detail).toContainText("Conforme");
    await expect(detail).toContainText("Attribution");
    await expect(detail).toContainText("Autorisé à attribuer");

    const submit = dialog.getByRole("button", { name: "Assigner l’actif" });
    await expect(submit).toBeEnabled();
    await submit.click();
    expect(submitted).toBeTruthy();
  } finally {
    await context.close();
  }
});
